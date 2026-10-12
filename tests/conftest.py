import pytest
from sqlalchemy import text

from amrit_adapter.db import make_engine, migrate, now_iso

BRAND = "amrit_dairy"


@pytest.fixture()
def engine():
    eng = make_engine("sqlite://")  # in-memory, fresh per test
    migrate(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO brand(brand_id, display_name, store_domain) VALUES (:b,'Amrit Dairy','kmaz1y-dq.myshopify.com')"), {"b": BRAND})
    return eng


def mk_contact(conn, cid="c1"):
    conn.execute(text("INSERT INTO contact(contact_id, brand_id, created_at) VALUES (:c,:b,:t)"), {"c": cid, "b": BRAND, "t": now_iso()})
    return cid


def mk_order(conn, key="amrit_dairy:shopify:order:1", contact="c1", total=350000, fin="unpaid", mode="prepaid", cod=None, number="#1150"):
    conn.execute(text("""INSERT INTO order_ref(order_key, brand_id, source, source_order_id, order_number, contact_id, currency, total_minor,
                        paid_minor, financial_state, fulfilment_state, payment_mode, cod_decision, updated_at)
                        VALUES (:k,:b,'shopify','1',:n,:c,'INR',:t,0,:f,'unfulfilled',:m,:cod,:u)"""),
                 {"k": key, "b": BRAND, "n": number, "c": contact, "t": total, "f": fin, "m": mode, "cod": cod, "u": now_iso()})
    return key
