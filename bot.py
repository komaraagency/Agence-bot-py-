import os
import io
import re
import sys
import json
import base64
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

if not TELEGRAM_TOKEN: sys.exit("ERREUR: TELEGRAM_TOKEN manquant.")
if not GEMINI_API_KEY: sys.exit("ERREUR: GEMINI_API_KEY manquant.")

KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.json")
DEMANDES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demandes_clients.json")

try:
    with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
        KNOWLEDGE = json.load(f)
    SYSTEM_PROMPT = KNOWLEDGE.get("system_prompt", "Tu es Komara Agency.")
    print(f"✅ knowledge.json chargé pour {KNOWLEDGE.get('agence')}")
except Exception as e:
    sys.exit(f"ERREUR FATALE: knowledge.json invalide — {e}")

menu_keyboard = [["Vos services", "Tarifs", "Commander"],["Portfolio", "Contact", "Vidéo IA"]]
reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)

async def safe_reply(update: Update, text: str):
    normalized_text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    try:
        await update.message.reply_text(normalized_text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def save_demande(demande: dict):
    try:
        demandes = []
        if os.path.exists(DEMANDES_FILE):
            with open(DEMANDES_FILE, "r", encoding="utf-8") as f: demandes = json.load(f)
        demande["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        demandes.append(demande)
        with open(DEMANDES_FILE, "w", encoding="utf-8") as f: json.dump(demandes, f, ensure_ascii=False, indent=2)
    except Exception as e: print(f"Erreur sauvegarde: {e}")

# FIX 1: BON NOM DE MODELE GEMINI
async def generate_image_with_gemini(prompt: str, image_bytes: bytes = None):
    final_prompt = f"{SYSTEM_PROMPT}\n\nDEMANDE CLIENT: {prompt}"
    parts = [{"text": final_prompt}]
    if image_bytes:
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64_image}})

    payload = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0.9}}
    # CHANGEMENT ICI: 2.5 -> 2.0-flash-exp
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent?key={GEMINI_API_KEY}"
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client: # FIX 2: Timeout 120s pour images
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            for candidate in data.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    if "inline_data" in part: return base64.b64decode(part["inline_data"]["data"]), None
                    if "text" in part: return None, part["text"]
    except httpx.HTTPStatusError as e: # FIX 3: Meilleur message d'erreur
        return None, f"Erreur API: {e.response.status_code} - {e.response.text}"
    except Exception as e: return None, str(e)
    return None, "Aucune image générée"

# MENU
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if text == "Vos services":
        services = "\n".join([f"- {s['nom']}: {s['prix']}" for s in KNOWLEDGE['services']])
        await safe_reply(update, f"*Nos Services:*\n\n{services}")
    elif text == "Tarifs": await safe_reply(update, "Nos prix commencent à 100.000 GNF. Dites-moi quel service pour un devis exact.")
    elif text == "Commander": await start_order_flow(update, context)
    elif text == "Contact": await safe_reply(update, f"WhatsApp: {KNOWLEDGE['contact']['whatsapp']}\nEmail: {KNOWLEDGE['contact']['email']}")
    elif text == "Portfolio": await safe_reply(update, f"Voir nos créations: {KNOWLEDGE['contact']['portfolio']}")
    elif text == "Vidéo IA": await safe_reply(update, "Service Vidéo IA: Sur demande. Décrivez votre vidéo.")

# COMMANDE
async def start_order_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    services = KNOWLEDGE.get("services", [])
    lignes = [f"{i+1}. {s['nom']} — {s['prix']}" for i, s in enumerate(services)]
    await safe_reply(update, "Quel service vous intéresse?\n\n" + "\n".join(lignes) + "\n\nRépondez avec le NUMÉRO.")
    context.user_data["order_state"] = "service"

async def handle_order_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    state = context.user_data.get("order_state")
    try: # FIX 4: Anti-crash si mauvais numéro
        if state == "service" and text.isdigit():
            idx = int(text) - 1
            service = KNOWLEDGE['services'][idx]
            context.user_data["order_data"] = {"service": service['nom']}
            context.user_data["order_state"] = "details"
            await safe_reply(update, f"Parfait: {service['nom']}. Décrivez votre projet:")
        elif state == "details":
            context.user_data["order_data"]["details"] = text
            context.user_data["order_state"] = "phone"
            await safe_reply(update, "Votre numéro WhatsApp:")
        elif state == "phone":
            data = context.user_data["order_data"]
            data["phone"] = text
            await save_demande(data)
            await safe_reply(update, f"Commande reçue! ✅ Nous vous contactons au {text}")
            context.user_data.clear()
    except (IndexError, KeyError):
        await safe_reply(update, "Numéro invalide. Veuillez taper un numéro de la liste.")
        context.user_data.clear()


# LE BOT COMPREND TOUT: TEXTE + PHOTO
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("order_state"):
        await handle_order_flow(update, context, update.message.text or "")
        return

    text = update.message.text if update.message.text else ""
    photo = update.message.photo[-1] if update.message.photo else None

    if text in ["Vos services", "Tarifs", "Commander", "Portfolio", "Contact", "Vidéo IA"]:
        await handle_menu(update, context, text)
        return
    
    # Si photo sans texte
    if photo and text == "":
        text = "améliore cette photo style KOMARA AGENCY, noir et or, luxe, 8K"

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    thinking = await update.message.reply_text("🎨 Je crée votre visuel KOMARA...")

    image_bytes = await photo.get_file().download_as_bytearray() if photo else None
    image_data, error = await generate_image_with_gemini(text, image_bytes)
    await thinking.delete()

    if image_data:
        await update.message.reply_photo(photo=io.BytesIO(image_data), caption=f"✨ {KNOWLEDGE['agence']}\n{KNOWLEDGE['slogan']}")
    else:
        await safe_reply(update, f"Je n'ai pas pu générer. Détail: {error}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, f"Bienvenue chez *{KNOWLEDGE['agence']}* 🇬🇳\n\n{KNOWLEDGE['description']}\n\nEnvoyez-moi juste votre idée ou une photo + ce que vous voulez changer.")

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, format, *args): pass

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))
    threading.Thread(target=start_health_server, daemon=True).start()
    print("Bot KOMARA démarré...")
    app.run_polling()

if __name__ == "__main__": main()
