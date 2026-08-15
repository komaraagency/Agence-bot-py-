# KomaraBot 🇬🇳
**Bot Telegram IA pour Komara Agency**

KomaraBot est l'assistant officiel de Komara Agency. Il répond aux clients 24/7, présente les services, donne les tarifs et génère des visuels IA avec Gemini 2.0 Flash.

Vision. Impact. Excellence.

---

### **🚀 Fonctionnalités**

| Commande | Description |
| --- | --- |
| `/start` | Affiche le menu principal avec clavier |
| `/genere [description]` | Génère une image IA 4K via Gemini 2.0 Flash |
| **Boutons Menu** | Vos services, Tarifs, Commander, Portfolio, Contact, Vidéo IA |
| **Chat Libre** | Pose n'importe quelle question. Le bot répond avec la base `knowledge.json` + Gemini |

### **🧠 Cerveau du Bot**

Le bot utilise 2 sources d'intelligence :

1.  **`knowledge.json`** : Base de données de Komara Agency. Services, prix, contact.
    Modifiable depuis GitHub → Le bot se met à jour au redéploiement.
2.  **Gemini 2.0 Flash** : Répond aux questions libres en utilisant uniquement les infos de `knowledge.json`.
    Pas d'hallucination. Ton chaleureux + émojis.

### **⚙️ Installation & Déploiement sur Railway**

#### **1. Variables d'environnement requises**
Va dans Railway → `Variables` et ajoute :
