"""T09/T10 (COD geometry) and T20/T21/T22 (shipment states)."""
import json

from sqlalchemy import text

from amrit_adapter import geofence, shipments
from tests.conftest import mk_contact, mk_order

# Synthetic TEST geometry only (a unit square near 0,0). Real Amrit zones require owner GeoJSON.
TEST_FC = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {"zone_id": "A", "owner_approved": True},
         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}},
        {"type": "Feature", "properties": {"zone_id": "B", "owner_approved": True, "radius_km": 10},
         "geometry": {"type": "Point", "coordinates": [5.0, 5.0]}},
        {"type": "Feature", "properties": {"zone_id": "C", "owner_approved": False},
         "geometry": {"type": "Polygon", "coordinates": [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]]}},
    ],
}


def _zones(tmp_path):
    p = tmp_path / "z.geojson"; p.write_text(json.dumps(TEST_FC))
    return geofence.load_zones(p, "test-v1")


def test_T09_inside_outside_boundary_decisions(tmp_path):
    z = _zones(tmp_path)
    assert geofence.decide_cod(z, 0.5, 0.5, "customer_confirmed").decision == "approved_zone_A"
    assert geofence.decide_cod(z, 1.0, 0.5, "customer_confirmed").decision == "approved_zone_A"      # boundary inclusive, documented
    assert geofence.decide_cod(z, 2.0, 2.0, "customer_confirmed").decision == "not_approved"
    assert geofence.decide_cod(z, 5.05, 5.0, "customer_confirmed").decision == "approved_zone_B"     # ~5.5 km from centre
    assert geofence.decide_cod(z, 10.5, 10.5, "customer_confirmed").decision == "not_approved"       # zone C not owner_approved


def test_T10_missing_geometry_or_location_never_auto_approves(tmp_path):
    missing = geofence.load_zones(tmp_path / "nope.geojson", "v")
    d = geofence.decide_cod(missing, 0.5, 0.5, "customer_confirmed")
    assert d.decision == "not_approved" and d.next_step == "prepaid"
    z = _zones(tmp_path)
    assert geofence.decide_cod(z, None, None, "customer_confirmed").decision == "not_approved"
    assert geofence.decide_cod(z, 0.5, 0.5, "geocoded").decision == "manual_review"      # geocoder guess is not enough
    # same object used by chat and checkout => identical decision
    a = geofence.decide_cod(z, 0.5, 0.5, "customer_confirmed"); b = geofence.decide_cod(z, 0.5, 0.5, "customer_confirmed")
    assert a == b


def test_T20_awb_or_pickup_scheduled_is_not_picked_up(engine):
    with engine.begin() as c:
        mk_contact(c); k = mk_order(c)
        shipments.ensure_shipment(c, "s1", k, "AWB1", "X")
        u = shipments.apply_event(c, "s1", "MANIFEST GENERATED", "5", "2026-10-10T10:00:00", "static_token_hint")
        assert u.new_state == "label_created" and u.customer_notify is False
        u = shipments.apply_event(c, "s1", "Pickup Scheduled", None, "2026-10-10T11:00:00", "static_token_hint")
        assert u.new_state == "pickup_scheduled" and u.customer_notify is False
        u = shipments.apply_event(c, "s1", "PICKED UP", "42", "2026-10-10T15:00:00", "static_token_hint")
        assert u.new_state == "picked_up" and u.customer_notify is False     # hint-level auth => verify via API first
        u = shipments.apply_event(c, "s1", "PICKED UP", "42", "2026-10-10T15:00:00", "static_token_hint")
        assert u.reason == "duplicate event"


def test_T21_out_of_order_transit_cannot_replace_delivered(engine):
    with engine.begin() as c:
        mk_contact(c); k = mk_order(c)
        shipments.ensure_shipment(c, "s2", k, "AWB2", "X")
        shipments.apply_event(c, "s2", "Delivered", None, "2026-10-12T10:00:00", "verified_api")
        u = shipments.apply_event(c, "s2", "In Transit", "18", "2026-10-11T10:00:00", "verified_api")
        assert u.applied is False and u.new_state == "delivered"
        assert c.execute(text("SELECT state FROM shipment_ref WHERE shipment_key='s2'")).scalar() == "delivered"


def test_T22_ndr_opens_human_task_and_unknown_status_is_exception(engine):
    with engine.begin() as c:
        mk_contact(c); k = mk_order(c)
        shipments.ensure_shipment(c, "s3", k, "AWB3", "X")
        u = shipments.apply_event(c, "s3", "Undelivered", None, "2026-10-12T10:00:00", "verified_api")
        assert u.new_state == "delivery_exception"
        assert c.execute(text("SELECT COUNT(*) FROM support_case WHERE case_type='ndr' AND owner_team='dispatch'")).scalar() == 1
        u2 = shipments.apply_event(c, "s3", "SOME NEW COURIER CODE", "999", "2026-10-12T11:00:00", "verified_api")
        assert u2.applied is False and u2.customer_notify is False
