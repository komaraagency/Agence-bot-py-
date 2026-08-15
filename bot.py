import os
import io
import sys
import json
import base64
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# 1. LIRE LES CLÉS DEPUIS RAILWAY (Ne plus les mettre en dur ici!)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Vérification au démarrage — crash clair plutôt qu'erreur cryptique plus tard
if not TELEGRAM_TOKEN:
    sys.exit("ERREUR: TELEGRAM_TOKEN manquant dans les variables d'environnement Railway.")
if not GEMINI_API_KEY:
    sys.exit("ERREUR: GEMINI_API_KEY manquant dans les variables d'environnement Railway.")

# 2. CHARGER LA BASE DE CONNAISSANCES
KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.json")
try:
    with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
        KNOWLEDGE = json.load(f)
    print(f"✅ knowledge.json chargé ({len(KNOWLEDGE.get('services', []))} services)")
except FileNotFoundError:
    print("⚠️ knowledge.json introuvable — le bot fonctionnera sans base de connaissances")
    KNOWLEDGE = None
except json.JSONDecodeError as e:
    print(f"⚠️ knowledge.json invalide: {e}")
    KNOWLEDGE = None

# Menu clavier — "Vos services" ajouté
menu_keyboard = [
    ["Vos services", "Tarifs", "Commander"],
    ["Portfolio", "Contact", "Vidéo IA"],
]
reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)

# --- Mini serveur HTTP pour le health check Railway ---
# Railway exige que l'app écoute sur $PORT, sinon le déploiement est marqué "failed".
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - KomaraBot is running")

    def log_message(self, format, *args):
        pass # silence les logs HTTP inutiles

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health check server listening on port {port}")
    server.serve_forever()

# --- Appel à Gemini (texte) avec la base de connaissances ---

async def ask_gemini_with_knowledge(question: str) -> str:
    """
    Envoie la question à Gemini avec la base de connaissances en contexte.
    Retourne une réponse textuelle propre.
    """
    knowledge_str = json.dumps(KNOWLEDGE, ensure_ascii=False) if KNOWLEDGE else "{}"

    system_prompt = (
        "Tu es Komara Agency 🇬🇳, une agence digitale basée en Guinée. "
        "Réponds aux questions des clients en utilisant UNIQUEMENT ces informations:\n\n"
        f"{knowledge_str}\n\n"
        "Règles:\n"
        "- Réponds de manière claire, professionnelle et concise\n"
        "- Si l'info n'est pas dans la base, dis-le honnêtement\n"
        "- Utilise des émojis légèrement pour un ton chaleureux\n"
        "- Réponds toujours en français\n"
        "- Termine parfois par le slogan: Vision. Impact. Excellence."
    )

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )

    payload = {
        "contents": [
            {"role": "user", "parts": [{"
