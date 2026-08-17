import os
import io
import sys
import json
import base64
import threading
import random
from urllib.parse import quote
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN:
    sys.exit("ERREUR: TELEGRAM_TOKEN manquant.")
if not GEMINI_API_KEY:
    sys.exit("ERREUR: GEMINI_API_KEY manquant.")

KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.json")
EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")

# Libellés lisibles pour chaque fichier d'exemple (utilisés dans le menu /exemples).
# Fallback automatique sur le nom de fichier si un nouveau fichier n'est pas listé ici.
EXAMPLE_LABELS = {
    "portrait_studio_homme.jpg": "Portrait studio (bonnet noir)",
    "flyer_tarifs_komara.jpg": "Flyer tarifs",
    "portrait_tenue_traditionnelle.jpg": "Portrait tenue traditionnelle",
    "lifestyle_cuisine_couple.jpg": "Lifestyle cuisine",
    "pub_business_24_7.jpg": "Pub \"Business 24/7\"",
    "scene_the_marocain.jpg": "Scène thé marocain",
    "portrait_studio_bonnet_ka.jpg": "Portrait studio (logo KA)",
    "creatif_bouteille_ndine_komara.jpg": "Visuel créatif original",
}

# Script de génération et retouche d'image — guide le comportement du bot
IMAGE_GENIUS_PROMPT = """Tu es Komara Agency, bot génératif d'image et retouche photo professionnelle.

MISSION
Créer des images originales et retoucher des photos uploadées avec précision, fidélité et créativité. Objectif: visuels prêts pour réseaux sociaux, e-commerce, branding.

COMPÉTENCES
1. Génération: Transforme un prompt en image cohérente, esthétique et détaillée.
2. Retouche: Modifie l'image uploadée sans déformer l'identité. Change fond, lumière, couleur, vêtements, supprime/ajoute des éléments.
3. Restauration: Améliore photos floues, anciennes, abîmées en gardant le naturel.
4. Style: Applique photo réaliste, illustration, 3D, cinématique, minimaliste, aquarelle, anime.

RÈGLES DE GÉNÉRATION
- Suis le prompt avec précision. Si un détail est flou, garde un rendu réaliste et neutre.
- Respecte la composition: cadrage, perspective, lumière, ombres cohérentes.
- Visages et mains: naturels, sans déformation ni doigt en trop.
- Texte: écris-le lisiblement uniquement si demandé.
- Vise la meilleure qualité sans artefact ni flou excessif.

PRINCIPES DE RETOUCHE
1. Fidélité: Ne change que ce qui est demandé. Garde l'identité et l'expression.
2. Réalisme: Les retouches doivent être invisibles. Pas d'effet plastique.
3. Cohérence: Lumière, couleur, grain doivent matcher avec l'original.
4. Non-destructif: Préserve la qualité et les détails importants.

STYLES
Photo réaliste, portrait studio, lifestyle, produit e-commerce, illustration vectorielle, 3D, cinématique, noir et blanc, vintage.
Par défaut, utilise photo réaliste haute qualité.

INTERDITS
- Images sexuelles impliquant des mineurs. Refus immédiat.
- Deepfake trompeur, harcèlement, usurpation d'identité.
- Contenu violent, gore, haineux, illégal.
- Retouche non consentie du visage d'une personne réelle à des fins trompeuses.
Si la demande viole ces règles, refuse et propose une alternative sûre.

FLUX DE TRAVAIL
1. Analyse le prompt ou l'image uploadée.
2. Si info manquante, pose 1 question courte sur style, cadrage, couleur.
3. Exécute la génération ou retouche.
4. Propose 1-2 variantes: "Veux-tu plus lumineux?", "Autre fond?".

FORMAT DE RÉPONSE
Sois concis. Pour une retouche, décris en 1 ligne ce qui a changé.
Ex: "Fond changé en blanc studio, lumière adoucie, peau retouchée naturellement."
Termine par une proposition d'amélioration."""
try:
    with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
        KNOWLEDGE = json.load(f)
    print(f"✅ knowledge.json chargé pour {KNOWLEDGE.get('agence')}")
except Exception as e:
    # On échoue vite et clairement plutôt que de continuer avec KNOWLEDGE=None
    # et planter plus tard sur un TypeError obscur (NoneType is not subscriptable)
    sys.exit(f"ERREUR FATALE: knowledge.json invalide — {e}")

menu_keyboard = [
    ["Vos services", "Tarifs", "Commander"],
    ["Portfolio", "Contact", "Vidéo IA"],
]
reply_markup = ReplyKeyboardMarkup(menu_keyboard, resize_keyboard=True)

async def safe_reply(update: Update, text: str):
    """Le prompt Gemini demande du **gras** — on essaie en Markdown, et si Telegram
    rejette la syntaxe (caractères non échappés dans un prix/texte), on retombe sur
    du texte brut plutôt que de planter silencieusement sans répondre."""
    try:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(text, reply_markup=reply_markup)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - KomaraBot is running")
    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health check server listening on port {port}")
    server.serve_forever()

async def ask_gemini_with_knowledge(question: str) -> str:
    knowledge_str = json.dumps(KNOWLEDGE, ensure_ascii=False, indent=2) if KNOWLEDGE else "{}"

    system_prompt = (
        f"Tu es KomaraBot, l'assistant commercial de {KNOWLEDGE['agence']} {KNOWLEDGE['pays']}.\n"
        f"Slogan: {KNOWLEDGE['slogan']}\nMission: {KNOWLEDGE['mission']}\n\n"
        f"BASE DE CONNAISSANCES COMPLÈTE:\n{knowledge_str}\n\n"
        "CAPACITÉ SPÉCIALE DU BOT (très important):\n"
        "Tu peux générer une VRAIE image de démonstration directement dans ce chat Telegram. "
        "Si le client veut voir un exemple, une démo, ou un aperçu visuel, dis-lui de taper "
        "'/genere' suivi d'une description (ex: /genere affiche pour un restaurant africain moderne). "
        "Ne le renvoie JAMAIS vers un humain pour ça — c'est une fonctionnalité que TU as.\n\n"
        "MISSION:\n"
        "1. Sois le meilleur commercial. Accueille, conseille, et vends les services.\n"
        "2. Réponds d'abord avec les infos de la BASE DE CONNAISSANCES.\n"
        "3. Si info manquante (hors démo image), dis: 'Je vous mets en contact avec un expert' et donne le WhatsApp.\n"
        "RÈGLES:\n"
        "- Français professionnel, vouvoiement\n"
        "- Réponse courte, structurée avec **gras** et listes •\n"
        "- 2 émojis max\n"
        "- Termine par un appel à l'action: 'Voulez-vous un devis?' ou 'Contact: +212701986219'\n"
        "- Cite le slogan 1 fois sur 3"
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": f"{system_prompt}\n\nQuestion du client: {question}"}]}],
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 1000},
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=30.0)
            response.raise_for_status()
            data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "".join([p["text"] for p in parts if "text" in p]).strip()
    except Exception as e:
        return f"Désolé, une erreur est survenue. Contactez-nous directement: {KNOWLEDGE['contact']['whatsapp']}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texte = f"""Salut! Je suis KomaraBot 🇬🇳
Assistant officiel de {KNOWLEDGE['agence']}
{KNOWLEDGE['slogan']}

Je peux vous aider à:
• Générer des images IA (/genere + description)
• Retoucher vos photos (envoyez une photo + vos instructions)
• Avoir un devis pour site web/logo
• Connaître nos services

Commandes:
/genere + description → créer une image IA
/exemples → voir nos réalisations
Photo + texte → retoucher une photo

Ou tapez sur un bouton ci-dessous 👇"""
    await update.message.reply_text(texte, reply_markup=reply_markup)

# Mots-clés qui indiquent une demande d'image même sans /genere explicite.
IMAGE_KEYWORDS = [
    "génère", "genere", "générer", "generer", "crée une image", "créer une image",
    "cree une image", "creer une image", "fais une image", "fait une image",
    "photo de", "image de", "logo pour", "logo de", "affiche pour", "affiche de",
    "visuel pour", "visuel de", "design pour", "montage photo", "colorisation",
    "coloriser", "photoréaliste", "photorealiste", "upscale", "rendu final",
    "arrière-plan", "background", "retouche photo",
]

def _should_generate_image(text: str) -> bool:
    """Règle de routage: prompt long (>25 mots) OU mots-clés précis -> génération
    d'image automatique, même si le client n'a pas tapé /genere."""
    if len(text.split()) > 25:
        return True
    lowered = text.lower()
    return any(k in lowered for k in IMAGE_KEYWORDS)

async def generate_and_send_image(update: Update, prompt: str):
    """Génère et envoie l'image en suivant STRICTEMENT le prompt du client —
    on n'y ajoute que des mots-clés de qualité, jamais de style imposé qui
    écraserait son intention réelle."""
    await update.message.reply_text("Création de votre visuel IA en cours... ⏳ 20s", reply_markup=reply_markup)

    # Pollinations.ai — gratuit, sans clé API. httpx async: rien ne bloque l'event loop.
    clean_prompt = prompt.strip()
    enhanced_prompt = f"{clean_prompt}, high quality, professional, detailed, 4k"
    image_url = f"https://image.pollinations.ai/prompt/{quote(enhanced_prompt)}?width=1024&height=1024&nologo=true&seed={abs(hash(clean_prompt)) % 1000000}"

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(image_url, timeout=60.0)
        response.raise_for_status()
        photo = io.BytesIO(response.content)
        photo.name = "komara_image.png"
        await update.message.reply_photo(
            photo,
            caption="Voici votre visuel selon votre description.\n\nVoulez-vous l'utiliser pour une pub? Contactez-nous.",
            reply_markup=reply_markup,
        )
    except Exception as e:
        print(f"⚠️ Erreur génération image: {e}")
        await update.message.reply_text(
            f"Désolé, la génération a échoué. Réessayez avec une description plus courte, "
            f"ou contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
            reply_markup=reply_markup,
        )

async def genere(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Ex: /genere affiche publicitaire pour Komara Agency", reply_markup=reply_markup)
        return
    await generate_and_send_image(update, prompt)

def _list_example_files():
    valid_exts = (".jpg", ".jpeg", ".png", ".webp")
    try:
        return sorted(
            f for f in os.listdir(EXAMPLES_DIR)
            if f.lower().endswith(valid_exts) and os.path.isfile(os.path.join(EXAMPLES_DIR, f))
        )
    except Exception:
        return []

def _label_for(filename: str) -> str:
    if filename in EXAMPLE_LABELS:
        return EXAMPLE_LABELS[filename]
    # Fallback: transforme le nom de fichier en libellé lisible
    name = os.path.splitext(filename)[0].replace("_", " ").replace("-", " ")
    return name.capitalize()

async def exemples(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche un MENU des exemples disponibles (texte uniquement, aucune photo
    envoyée à ce stade). Le client choisit un numéro -> le bot envoie UNE SEULE
    photo correspondante, pas plusieurs, pour ne pas spammer le chat."""
    image_files = _list_example_files()

    if not image_files:
        await update.message.reply_text(
            "Aucun exemple disponible pour le moment. 📸\n"
            "Nos réalisations arrivent bientôt !\n\n"
            f"En attendant, contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
            reply_markup=reply_markup,
        )
        return

    # On mélange l'ordre affiché pour varier ce que les clients voient à chaque appel.
    random.shuffle(image_files)

    lignes = [f"{i+1}. {_label_for(f)}" for i, f in enumerate(image_files)]
    texte = (
        f"Voici nos modèles de réalisations {KNOWLEDGE['agence']} 📸\n\n"
        + "\n".join(lignes)
        + "\n\nRépondez avec le NUMÉRO du modèle qui vous intéresse pour le voir en photo."
    )

    # On mémorise la liste proposée pour cet utilisateur, dans l'ordre affiché,
    # afin de savoir quelle photo envoyer quand il répond par un numéro.
    context.user_data["pending_examples"] = image_files

    await update.message.reply_text(texte, reply_markup=reply_markup)

async def handle_example_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Si un menu /exemples est en attente pour cet utilisateur, tente de faire
    correspondre sa réponse (numéro ou nom) à UN SEUL exemple et l'envoie.
    Retourne True si la réponse a été traitée ici (pour arrêter le routage normal)."""
    pending = context.user_data.get("pending_examples")
    if not pending:
        return False

    choice = text.strip()
    selected = None

    # Correspondance par numéro (1-indexé)
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(pending):
            selected = pending[idx]

    # Correspondance par nom/libellé approximatif
    if selected is None:
        lowered = choice.lower()
        for f in pending:
            if lowered in _label_for(f).lower() or lowered in f.lower():
                selected = f
                break

    if selected is None:
        # Réponse non reconnue: on redemande poliment, sans spammer de photo,
        # et on garde le menu actif pour un nouvel essai.
        await update.message.reply_text(
            "Je n'ai pas reconnu ce choix. Répondez avec le NUMÉRO du modèle "
            "affiché dans la liste ci-dessus (ex: 1).",
            reply_markup=reply_markup,
        )
        return True

    # On envoie EXACTEMENT une photo, puis on nettoie l'état pour ce client.
    context.user_data.pop("pending_examples", None)
    img_path = os.path.join(EXAMPLES_DIR, selected)
    try:
        with open(img_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=f"{_label_for(selected)} — Réalisé par {KNOWLEDGE['agence']} ✨",
                reply_markup=reply_markup,
            )
        await update.message.reply_text(
            "Voulez-vous un projet similaire? Tapez /genere + votre idée pour un visuel sur-mesure. 🎨",
            reply_markup=reply_markup,
        )
    except Exception as e:
        print(f"⚠️ Erreur envoi exemple {selected}: {e}")
        await update.message.reply_text(
            f"Désolé, une erreur est survenue. Contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
            reply_markup=reply_markup,
        )
    return True

async def edit_photo_with_gemini_image(photo_bytes: bytes, user_instructions: str):
    """Retouche RÉELLE de la photo — contrairement à Pollinations (texte -> nouvelle image
    générée from scratch, donc incohérente avec l'original), Gemini 2.5 Flash Image (Nano
    Banana) édite DIRECTEMENT les pixels de la photo fournie. La pose, le décor, l'identité
    du sujet et le cadrage sont donc préservés — seuls les éléments demandés par le client
    changent. C'est le fix pour le problème 'images incohérentes qui ne suivent pas le prompt'.

    Retourne (image_bytes, description_texte) ou (None, None) en cas d'échec."""
    b64_image = base64.b64encode(photo_bytes).decode("utf-8")

    edit_prompt = (
        f"{IMAGE_GENIUS_PROMPT}\n\n"
        "Voici une photo à retoucher. Applique STRICTEMENT et UNIQUEMENT les instructions "
        "du client ci-dessous. Ne change RIEN d'autre: garde exactement la même pose, le même "
        "cadrage, la même composition, le même arrière-plan (sauf si demandé), la même identité "
        "du sujet. Aucun déplacement, repositionnement, ajout ou suppression non demandé. "
        "Aucun recadrage sauf si explicitement demandé.\n\n"
        f"Instructions du client: {user_instructions}\n\n"
        "Après l'image, ajoute UNE ligne en français décrivant ce qui a été changé."
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": edit_prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}
            ]
        }],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=60.0)
            response.raise_for_status()
            data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        image_bytes = None
        description = None
        for part in parts:
            # L'API renvoie inlineData (camelCase) en JSON, mais on vérifie les deux
            # variantes par sécurité selon la version d'API.
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                image_bytes = base64.b64decode(inline["data"])
            elif part.get("text"):
                description = part["text"].strip()
        return image_bytes, description
    except Exception as e:
        print(f"⚠️ Erreur édition Gemini Image: {e}")
        return None, None

async def analyze_photo_with_gemini(photo_bytes: bytes, user_instructions: str) -> str:
    """Utilise Gemini Vision pour analyser la photo uploadée et produire un prompt
    de génération détaillé qui combine la description de l'image + les instructions
    du client. Le résultat alimente Pollinations.ai pour la génération finale."""
    b64_image = base64.b64encode(photo_bytes).decode("utf-8")

    analyze_prompt = (
        f"{IMAGE_GENIUS_PROMPT}\n\n"
        "Tu vas recevoir une image uploadée par un client. Analyse-la en détail: "
        "sujet, cadrage, couleurs, lumière, arrière-plan, vêtements, style, ambiance. "
        "Ensuite, combine cette analyse avec les instructions du client pour produire "
        "UN SEUL prompt en anglais, extrêmement détaillé, qui servira à générer une "
        "nouvelle image via un modèle text-to-image.\n\n"
        "RÈGLES CRITIQUES:\n"
        "- Le prompt final doit décrire EXACTEMENT ce que le client veut voir\n"
        "- Si le client demande de changer le fond, décris le NOUVEAU fond\n"
        "- Si le client demande de changer les couleurs, décris les NOUVELLES couleurs\n"
        "- Garde l'identité du sujet (âge, genre, pose) sauf si le client demande de changer\n"
        "- Ajoute: 'high quality, professional, detailed, 4k, photorealistic'\n"
        "- Ne JAMAIS ajouter de style non demandé par le client\n\n"
        f"Instructions du client: {user_instructions if user_instructions else 'Améliorer cette photo avec un rendu professionnel'}\n\n"
        "Réponds UNIQUEMENT avec le prompt en anglais, sans introduction ni explication."
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": analyze_prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}
            ]
        }],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 800},
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=45.0)
            response.raise_for_status()
            data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "".join([p["text"] for p in parts if "text" in p]).strip()
    except Exception as e:
        print(f"⚠️ Erreur analyse Gemini Vision: {e}")
        return None

async def describe_retouche_result(user_instructions: str, photo_description: str) -> str:
    """Génère une courte description en français de ce qui a été fait, style:
    'Fond changé en blanc studio, lumière adoucie, peau retouchée naturellement.'"""
    prompt = (
        f"{IMAGE_GENIUS_PROMPT}\n\n"
        "Le client a demandé une retouche. Voici ses instructions et la description de la photo originale. "
        "Décris en UNE ligne en français ce qui a été changé, puis propose une amélioration.\n\n"
        f"Instructions: {user_instructions}\n"
        f"Photo analysée: {photo_description[:500]}\n\n"
        "Format: description courte + une question d'amélioration."
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 200},
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=30.0)
            response.raise_for_status()
            data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "".join([p["text"] for p in parts if "text" in p]).strip()
    except Exception as e:
        print(f"⚠️ Erreur description retouche: {e}")
        return "Retouche effectuée selon vos instructions."

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère les photos envoyées par le client — avec ou sans instructions en caption.
    Avec caption: analyse la photo + applique les instructions -> génère le résultat.
    Sans caption: demande ce que le client veut faire avec sa photo."""
    try:
        # Récupérer le texte (caption) s'il y en a un
        user_instructions = update.message.caption or ""

        if not user_instructions:
            await update.message.reply_text(
                "📸 Photo reçue! Décrivez-moi ce que vous voulez faire avec:\n"
                "• Changer le fond\n"
                "• Retoucher la lumière/les couleurs\n"
                "• Améliorer la qualité\n"
                "• Changer de style\n"
                "• Ajouter/supprimer un élément\n\n"
                "Répondez simplement avec vos instructions.",
                reply_markup=reply_markup,
            )
            return

        # Vérifier les interdits
        forbidden = ["mineur", "deepfake", "usurpation", "harcèlement", "violence", "gore"]
        if any(w in user_instructions.lower() for w in forbidden):
            await update.message.reply_text(
                "Je ne peux pas traiter cette demande. Elle va à l'encontre de mes règles d'utilisation. "
                "Je peux vous proposer une alternative sûre — décrivez votre besoin différemment. 🙏",
                reply_markup=reply_markup,
            )
            return

        await update.message.reply_text("Analyse de votre photo et retouche en cours... ⏳ 20-30s", reply_markup=reply_markup)

        # 1. Télécharger la photo depuis Telegram
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = bytes(await photo_file.download_as_bytearray())

        # 2. Retouche RÉELLE via Gemini 2.5 Flash Image — édite les pixels de la
        # photo fournie au lieu de régénérer une image complètement différente.
        # C'est la méthode prioritaire: elle suit fidèlement le prompt et préserve
        # la photo originale (pose, décor, identité).
        image_bytes, description = await edit_photo_with_gemini_image(photo_bytes, user_instructions)

        if image_bytes:
            result_photo = io.BytesIO(image_bytes)
            result_photo.name = "komara_retouche.png"
            caption = f"{description or 'Retouche effectuée selon vos instructions.'}\n\nVeux-tu une variante? Contactez-nous: {KNOWLEDGE['contact']['whatsapp']}"
            await update.message.reply_photo(result_photo, caption=caption, reply_markup=reply_markup)
            return

        # 3. Filet de sécurité: si l'édition Gemini échoue (quota, erreur réseau...),
        # on retombe sur l'ancienne méthode Pollinations pour ne jamais laisser le
        # client sans réponse — priorité à la stabilité du bot.
        print("⚠️ Édition Gemini Image indisponible, repli sur Pollinations.")
        generation_prompt = await analyze_photo_with_gemini(photo_bytes, user_instructions)
        if not generation_prompt:
            generation_prompt = f"{user_instructions}, high quality, professional, detailed, 4k, photorealistic"

        image_url = f"https://image.pollinations.ai/prompt/{quote(generation_prompt[:1500])}?width=1024&height=1024&nologo=true&seed={abs(hash(generation_prompt)) % 1000000}"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(image_url, timeout=60.0)
        response.raise_for_status()
        result_photo = io.BytesIO(response.content)
        result_photo.name = "komara_retouche.png"

        fallback_description = await describe_retouche_result(user_instructions, generation_prompt)
        caption = (
            f"{fallback_description}\n\n⚠️ Résultat approximatif (mode secours). "
            f"Contactez-nous pour un rendu garanti fidèle: {KNOWLEDGE['contact']['whatsapp']}"
        )
        await update.message.reply_photo(result_photo, caption=caption, reply_markup=reply_markup)

    except httpx.HTTPStatusError as e:
        print(f"⚠️ Erreur HTTP génération: {e}")
        await update.message.reply_text(
            f"La génération a échoué. Réessayez avec une photo plus simple, "
            f"ou contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
            reply_markup=reply_markup,
        )
    except Exception as e:
        print(f"⚠️ Erreur handle_photo: {e}")
        await update.message.reply_text(
            f"Désolé, une erreur est survenue avec votre photo. 🙏 "
            f"Contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
            reply_markup=reply_markup,
        )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    try:
        # Si un menu /exemples est en attente pour ce client, on traite sa réponse
        # ici en priorité (un seul exemple envoyé), avant tout autre routage.
        if await handle_example_choice(update, context, text):
            return

        if text.lower() in ("exemple", "exemples", "/exemple", "/exemples"):
            await exemples(update, context)
            return

        if text == "Vos services":
            reponse = await ask_gemini_with_knowledge("Présente tous les services de Komara Agency avec prix et délais de façon commerciale.")
        elif text == "Tarifs":
            reponse = await ask_gemini_with_knowledge("Donne la liste des tarifs de tous les services.")
        elif text == "Commander":
            reponse = f"Parfait! Décrivez votre projet ici ou contactez directement notre équipe sur WhatsApp: {KNOWLEDGE['contact']['whatsapp_lien']}"
        elif text == "Contact":
            c = KNOWLEDGE['contact']
            reponse = f"📞 **Contactez {KNOWLEDGE['agence']}**\n\nWhatsApp: {c['whatsapp']}\nEmail: {c['email']}\nAdresse: {c['adresse']}\nPortfolio: {c['portfolio']}\n\n{KNOWLEDGE['slogan']}"
        elif text == "Portfolio":
            reponse = f"Découvrez nos réalisations ici 👇\n{KNOWLEDGE['contact']['portfolio']}"
        elif text == "Vidéo IA":
            reponse = await ask_gemini_with_knowledge("Parle du service Vidéo IA, prix et délais.")
        elif _should_generate_image(text):
            # Prompt long (>25 mots) ou mots-clés précis -> génération d'image
            # directe, sans exiger que le client tape /genere.
            await generate_and_send_image(update, text)
            return
        else:
            reponse = await ask_gemini_with_knowledge(text)
        await safe_reply(update, reponse)
    except Exception as e:
        # Filet de sécurité: le bot ne doit JAMAIS rester silencieux, même en cas
        # de bug imprévu. On log l'erreur et on répond quand même au client.
        print(f"⚠️ Erreur dans handle_menu: {e}")
        try:
            await update.message.reply_text(
                f"Désolé, un problème technique est survenu. 🙏 Contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
                reply_markup=reply_markup,
            )
        except Exception:
            pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Filet de sécurité ultime au niveau de l'application: si un handler plante
    d'une façon totalement imprévue (bug non couvert par les try/except internes),
    le bot répond quand même au client au lieu de rester silencieux."""
    print(f"⚠️ Erreur non gérée par l'application: {context.error}")
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                f"Désolé, un problème technique est survenu. 🙏 Contactez-nous: {KNOWLEDGE['contact']['whatsapp']}",
                reply_markup=reply_markup,
            )
    except Exception:
        pass

def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("genere", genere))
    app.add_handler(CommandHandler("exemples", exemples))
    app.add_handler(CommandHandler("exemple", exemples))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    app.add_error_handler(error_handler)
    print(f"Bot {KNOWLEDGE['agence']} lancé...")
    # run_polling() gère lui-même le nettoyage du webhook (delete_webhook) avant
    # de démarrer — pas besoin d'appeler asyncio.run() nous-mêmes ici : ça créait
    # et fermait une boucle asyncio séparée, ce qui cassait la boucle que
    # run_polling() essaie ensuite de récupérer ("no current event loop").
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
