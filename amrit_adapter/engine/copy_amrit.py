"""Owner-authored WhatsApp copy (Amit Kumar, 19 Oct 2026, 'apply as it is'). Edit text HERE, not in state.py.
Prices are never written here: pack lines are rendered from live Shopify prices (+ owner-approved WhatsApp offers)."""
from __future__ import annotations

FOOTER = "हर घर अमृत 🌿"

# ---------------- contacts (owner, 19 Oct) ----------------
PHONE_ORDER = "7700004877"       # Amrit Dairy: order related
PHONE_COW = "8059593666"         # cow buy / sell
PHONE_HARSH = "9873033339"       # Harsh Chhikara; also Ayurvedic desi medicine (Baba Ji Ki Buti)
PHONE_BULK = "8570007678"        # bulk buy
BKB_SITE = "https://www.babajikibuti.com"
BKB_MAP = "https://maps.app.goo.gl/GRT3oGKbGrbQrJxF9?g_st=ac"
DAIRY_MAP = "https://maps.google.com/?q=Amrit+Dairy+Rajbhaya+Garhi+Brahmanan+Sonipat+Haryana+131001"


def box(title: str) -> str:
    return f"╭───────────────╮\n│ {title}\n╰───────────────╯"


# ---------------- 0. Welcome ----------------
WELCOME = (
    "🙏🏼 *राम राम जी, स्वागत है*\n\n"
    "अमृत फार्म से आपकी रसोई तक के सफ़र में\n\n"
    "यहाँ आपको मिलेगा शुद्ध देसी गाय के पवित्र दूध से तैयार किया गया *औषधि जैसा घी* "
    "और छोटी मधुमक्खियों का *औषधि जैसा जंगली शहद*।\n\n"
    "तो बताइए, क्या चाहिए?\n*विकल्प चुनें* 👇"
)
# (id, title ≤24, description ≤72) — order = the owner's numbering 1..7
MENU_ROWS = [
    ("product:ghee", "देसी गाय का घी", "गीर, साहीवाल, थारपारकर गायों के दूध से"),
    ("product:honey", "शुद्ध देसी शहद", "नेचुरली छोटी मधुमक्खियों से बना"),
    ("product:cow", "गाय खरीदने/बेचने के लिए", "जानकारी / टीम से बात"),
    ("info:bkb", "देशी दवाइयां", "Baba Ji Ki Buti Order"),
    ("track", "Order Track", "Order नंबर भेजें"),
    ("info:location", "Amrit Dairy Location", "Address & Location"),
    ("info:contact", "Contact Us", "Team, Bulk Order, Dairy Contact, Harsh Chhikara"),
]

# ---------------- 1. Ghee ----------------
GHEE_HEAD = (
    box("🧈 *AMRIT GHEE* 🧈") + "\n\n"
    "> 🐄 *Desi Cow Ghee*\n\n"
    "अमृत Dairy की गायों के दूध से\n*बिलोणा विधि से* तैयार।\n\n"
    "*Export Quality •* 🌿 Organic *Certified* *• औषधि स्वरूप*\n\n"
    "> 🧪 Lab Tested Quality\n> 🌿 साफ़ Handling & Packing\n> 🐄 Amrit Dairy से Direct\n\n"
    "📦 *पैक चुनिए* 👇 (कीमत GST सहित)\n\n📦 *PACK & PRICE*\n\n"
)
GHEE_TAIL = "━━━━━━━━━━━━━━\n\n✓ Minimum Order — 1 Ltr\n\n> `1` • `SPECIAL GIR COW GHEE`\n> `2` • `CUSTOM QUANTITY`"
GHEE_EXTRA_ROWS = [("info:gir_ghee", "SPECIAL GIR COW GHEE", "गीर गाय का स्पेशल घी — टीम से पूछें"),
                   ("info:custom_qty", "CUSTOM QUANTITY", "अपनी मात्रा / Bulk Order")]

# ---------------- 2. Honey ----------------
HONEY_HEAD = (
    box("🍯 *AMRIT HONEY* 🍯") + "\n"
    "🌿 *Natural Raw Forest Honey*\n\n"
    "असली स्वाद और Lab Report के साथ — छोटी मधुमक्खियों का नेचुरल शहद 🌿\n\n"
    "*• एक्सपोर्ट Quality • औषधि जैसा शहद • साफ़ जानकारी*\n\n"
    "> 🧪 Lab Tested Quality\n> 🍯 Natural Raw Forest Honey\n> 🌿 Quality & Compliance Checked\n\n"
    "> *अपना Honey Pack चुनिए 👇*\n\n📦 *PACK & PRICE*\n\n"
)
HONEY_TAIL = "> ━━━━━━━━━━━━\n\n*हर घर अमृत* 🌿\n🛒"

# ---------------- 3. Cow ----------------
COW_ENQUIRY = (
    box("🐄 *COW ENQUIRY* 🐄") + "\n\n> 🙏 नमस्कार जी\n\nAmrit Dairy में हमारे यहाँ मुख्य रूप से—\n\n"
    "> • गीर\n\n> • साहीवाल\n\n> • हरियाणा देसी\n\n> • थारपारकर\n\n> • राठी\n\nनस्ल की देसी गायें हैं।\n\n"
    "━━━━━━━━━━━━━━\n\n> *आपकी Requirement क्या है ? 👇*\n\n"
    "> `1` • `COW ख़रीदनी है`\n\n> `2` • `COW बेचनी है`\n\n> `3` • `जानकारी लेनी है`\n\n"
    "━━━━━━━━━━━━━━\n\n*साफ़ जानकारी • सही Verification • भरोसे का व्यवहार*\n\n*Amrit Dairy* 🐄\n\n*हर घर अमृत* 🌿"
)
COW_BUTTONS = [("cow:buy", "COW ख़रीदनी है"), ("cow:sell", "COW बेचनी है"), ("cow:info", "जानकारी लेनी है")]

COW_BUY = (
    box("🐄 *COW BUY REQUEST*") + "\n\nबहुत अच्छा जी 🙏\n\nआपकी requirement समझने के लिए नीचे की basic details भेज दें—\n\n"
    "> 👤 *Name:*\n\n> 📱 *Mobile:*\n\n> 📍 *Village / City:*\n\n> 🏙 *District / State:*\n\n> 🐄 *Required Breed:*\n\n"
    "> 🥛 *Expected Milk / Day:*\n\n> 💰 *Approx. Budget:*\n\n> 📝 *Any Special Requirement:*\n\n━━━━━━━━━━━━━━\n\n"
    "Details मिलने के बाद हमारी team\n\nAvailable cow और आगे का process बताएगी।\n\n"
    "> *Final availability और price actual cow पर depend करेगा.*\n\n━━━━━━━━━━━━━━\n\n*Amrit Dairy Cow Desk* 🐄"
)
COW_SELL = (
    box("🐄 *COW SELL REQUEST*") + "\n\nगाय की सही जानकारी के लिए नीचे details भेजें 👇\n\n"
    "> 👤 *Owner Name:*\n\n> 📱 *Mobile Number:*\n\n> 📍 *Location:*\n\n> 🐄 *Breed:*\n\n> 🎂 *Approx. Age:*\n\n"
    "> 🥛 *Milk / Day:*\n\n> 🔄 *Lactation: ब्यात*\n\n> 🤰 *Pregnancy Status:*\n\n> 🐮 *Calf साथ है ? Male / Female*\n\n"
    "> 💰 *Expected Price:*\n\n📸 साथ में भेजें—\n\n> • Clear Side Photos\n\n> • Front / Back Photo\n\n> • Udder Photo\n\n"
    "> • Walking Video\n\n> • Milking Video\n\n> • Calf Photo, यदि साथ है\n\n━━━━━━━━━━━━━━\n\n"
    "*Final decision physical verification और records के बाद होगा.*\n\n*Amrit Dairy Cow Desk* 🐄"
)
# generated in the owner's style ("Ask and Generate Best")
COW_INFO = (
    box("🐄 *COW जानकारी*") + "\n\n🙏 जी, गायों से जुड़ी ज़रूरी जानकारी—\n\n"
    "> 🐮 *नस्लें:* गीर • साहीवाल • हरियाणा देसी • थारपारकर • राठी\n\n"
    "> 🔍 *Verification:* नस्ल, दूध, ब्यात और स्वास्थ्य की पुष्टि farm पर देखकर ही\n\n"
    "> 💰 *कीमत:* हर गाय की अलग — नस्ल, उम्र, दूध और ब्यात के हिसाब से\n\n"
    "> 🚚 *Transport:* पूरे भारत में व्यवस्था, ownership transfer के साथ\n\n"
    "> 🕖 *Farm Visit:* रोज़ सुबह 7 से रात 9 बजे — बिना appointment\n\n"
    "━━━━━━━━━━━━━━\n\n📞 *Cow Desk:* " + PHONE_COW + " (Call / WhatsApp)\n\n"
    "आगे क्या करना है? 👇\n\n*Amrit Dairy Cow Desk* 🐄"
)
COW_INFO_BUTTONS = [("cow:buy", "COW ख़रीदनी है"), ("cow:sell", "COW बेचनी है"), ("info:location", "Farm Location")]
COW_FORM_THANKS = (
    box("🐄 *DETAILS मिल गईं*") + "\n\nधन्यवाद जी 🙏\nआपकी details हमारी *Cow Desk* टीम तक पहुँच गई हैं —\n"
    "जल्द ही आपसे संपर्क करेंगे।\n\n📸 और Photos / Video हों तो यहीं भेज दीजिए।\n📞 जल्दी हो तो: " + PHONE_COW + "\n\n*Amrit Dairy Cow Desk* 🐄"
)

# ---------------- 4. Baba Ji Ki Buti (generated: best 3) ----------------
BKB = (
    box("🌿 *BABA JI KI BUTI* 🌿") + "\n\n🙏 *देशी आयुर्वेदिक दवाइयां*\n\n"
    "पारंपरिक जड़ी-बूटियों से तैयार देसी दवाइयां —\nअब घर बैठे ऑर्डर करें।\n\n━━━━━━━━━━━━━━\n\n"
    "> `1` • 🛒 *Order Now* — Website पर\n\n> `2` • 📍 *Address* — Location के साथ\n\n> `3` • 📞 *Contact* — " + PHONE_HARSH + "\n\n"
    "━━━━━━━━━━━━━━\n\n*Baba Ji Ki Buti* 🌿 • *हर घर अमृत*"
)
BKB_BUTTONS = [("info:bkb_order", "🛒 Order Now"), ("info:bkb_address", "📍 Address"), ("info:bkb_contact", "📞 Contact")]
BKB_ORDER = (box("🛒 *ORDER NOW*") + "\n\nBaba Ji Ki Buti की सभी देशी दवाइयां website पर 👇\n\n🔗 " + BKB_SITE +
             "\n\n✔️ सीधे ऑर्डर • 🔒 सुरक्षित भुगतान • 🚚 घर तक डिलीवरी\n\nकोई सवाल हो तो 📞 " + PHONE_HARSH + "\n\n*Baba Ji Ki Buti* 🌿")
BKB_ADDRESS = (box("📍 *BABA JI KI BUTI*") + "\n\n🏪 BABA JI KI BUTI PRIVATE LIMITED\n📍 2238/23, Market Road, Sector 23,\nSonipat, Haryana 131001\n\n"
               "🗺️ Location 👇\n" + BKB_MAP + "\n\n📞 " + PHONE_HARSH + "\n\n*Baba Ji Ki Buti* 🌿")
BKB_CONTACT = (box("📞 *CONTACT*") + "\n\n🌿 देशी आयुर्वेदिक दवाइयों के लिए\n\n📱 *" + PHONE_HARSH + "*\n_Call / WhatsApp_\n\n"
               "दवाई का नाम या अपनी परेशानी बताइए —\nहमारी टीम सही जानकारी देगी 🙏\n\n*Baba Ji Ki Buti* 🌿")

# ---------------- 6. Location (generated) ----------------
LOCATION = (
    box("📍 *AMRIT DAIRY LOCATION*") + "\n\n🐄 *Amrit Dairy Farm*\n"
    "📍 राजभया गढ़ी ब्राह्मणान, अमृत-नीर महल के पास,\nसोनीपत, हरियाणा 131001\n\n"
    "🗺️ Google Maps 👇\n" + DAIRY_MAP + "\n\n"
    "🕖 रोज़ सुबह 7 से रात 9 बजे\n✅ Farm Visit के लिए appointment की ज़रूरत नहीं\n\n📞 " + PHONE_ORDER + "\n\n*हर घर अमृत* 🌿"
)

# ---------------- 7. Contact Us (owner list) ----------------
CONTACT = (
    box("📞 *CONTACT US*") + "\n\n🙏 सही टीम से सीधे बात कीजिए—\n\n"
    "> `1` • 🧈 *Amrit Dairy — Order Related*\n>    📱 " + PHONE_ORDER + "\n\n"
    "> `2` • 🐄 *Cow Buy / Sell Related*\n>    📱 " + PHONE_COW + "\n\n"
    "> `3` • 👤 *Harsh Chhikara*\n>    📱 " + PHONE_HARSH + "\n\n"
    "> `4` • 🌿 *Ayurvedic Desi Medicine*\n>    📱 " + PHONE_HARSH + "\n\n"
    "> `5` • 📦 *Bulk Buy*\n>    📱 " + PHONE_BULK + "\n\n"
    "━━━━━━━━━━━━━━\n_Call / WhatsApp — सुबह 7 से रात 9 बजे_\n\n*हर घर अमृत* 🌿"
)

GIR_GHEE = (box("🧈 *SPECIAL GIR COW GHEE*") + "\n\n🐄 शुद्ध *गीर गाय* के दूध से बिलोणा विधि से तैयार स्पेशल घी।\n\n"
            "उपलब्धता और कीमत के लिए हमारी टीम आपसे अभी बात करेगी 🙏\n\n📞 " + PHONE_ORDER + "\n\n*हर घर अमृत* 🌿")
CUSTOM_QTY = (box("📦 *CUSTOM QUANTITY*") + "\n\nअपनी ज़रूरत की मात्रा लिखिए 👇\n_(जैसे: 2 Ltr, 15 Ltr, 25 Ltr)_\n\n"
              "📦 *Bulk Order* के लिए सीधे: 📱 " + PHONE_BULK + "\n\nहमारी टीम सही कीमत के साथ जवाब देगी 🙏\n\n*हर घर अमृत* 🌿")

INFO = {"bkb": BKB, "bkb_order": BKB_ORDER, "bkb_address": BKB_ADDRESS, "bkb_contact": BKB_CONTACT,
        "location": LOCATION, "contact": CONTACT, "gir_ghee": GIR_GHEE, "custom_qty": CUSTOM_QTY, "cow_info": COW_INFO}
