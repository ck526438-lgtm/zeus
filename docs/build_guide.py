"""Génère docs/Guide_Zeus_HFT.pdf (reportlab). Lancer : python docs/build_guide.py"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)
from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
for name, file in (("Sans", "DejaVuSans.ttf"), ("Sans-Bold", "DejaVuSans-Bold.ttf"),
                   ("Sans-Oblique", "DejaVuSans-Oblique.ttf"), ("Mono", "DejaVuSansMono.ttf")):
    pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / file)))
pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans-Oblique",
                              boldItalic="Sans-Bold")

NAVY = colors.HexColor("#0f1a2b")
GOLD = colors.HexColor("#c8901f")
GREY = colors.HexColor("#5b6574")
LIGHT = colors.HexColor("#f3f5f8")
RED = colors.HexColor("#b3261e")
GREEN = colors.HexColor("#1e7b4f")

ST = {
    "title": ParagraphStyle("title", fontName="Sans-Bold", fontSize=30, leading=36, textColor=NAVY,
                            alignment=TA_CENTER),
    "subtitle": ParagraphStyle("subtitle", fontName="Sans", fontSize=13, leading=18, textColor=GREY,
                               alignment=TA_CENTER),
    "h1": ParagraphStyle("h1", fontName="Sans-Bold", fontSize=17, leading=22, textColor=NAVY,
                         spaceBefore=6, spaceAfter=8),
    "h2": ParagraphStyle("h2", fontName="Sans-Bold", fontSize=12.5, leading=16, textColor=GOLD,
                         spaceBefore=10, spaceAfter=4),
    "body": ParagraphStyle("body", fontName="Sans", fontSize=10.5, leading=14.5, spaceAfter=4),
    "small": ParagraphStyle("small", fontName="Sans", fontSize=9, leading=12, textColor=GREY),
    "cell": ParagraphStyle("cell", fontName="Sans", fontSize=9.2, leading=12),
    "cellb": ParagraphStyle("cellb", fontName="Sans-Bold", fontSize=9.2, leading=12, textColor=colors.white),
    "code": ParagraphStyle("code", fontName="Mono", fontSize=8.8, leading=12, backColor=LIGHT,
                           borderPadding=(5, 6, 5, 6), spaceBefore=4, spaceAfter=8, leftIndent=4),
    "box": ParagraphStyle("box", fontName="Sans", fontSize=10, leading=14),
}


def P(text: str, style: str = "body") -> Paragraph:
    return Paragraph(text, ST[style])


def bullets(items: list[str]) -> ListFlowable:
    return ListFlowable([ListItem(P(i), leftIndent=12, value="•") for i in items], bulletType="bullet",
                        start="•", leftIndent=14, bulletFontName="Sans", bulletColor=GOLD)


def steps(items: list[str]) -> ListFlowable:
    return ListFlowable([ListItem(P(i), leftIndent=14) for i in items], bulletType="1",
                        leftIndent=16, bulletFontName="Sans-Bold", bulletColor=NAVY)


def code(text: str) -> Paragraph:
    return Paragraph(text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br/>"), ST["code"])


def table(rows: list[list[str]], widths: list[float]) -> Table:
    data = [[Paragraph(c, ST["cellb"]) for c in rows[0]]]
    data += [[Paragraph(c, ST["cell"]) for c in r] for r in rows[1:]]
    t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d5dae1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def callout(text: str, color: colors.Color = GOLD, title: str = "") -> Table:
    body = (f"<b>{title}</b><br/>" if title else "") + text
    t = Table([[Paragraph(body, ST["box"])]], colWidths=[170 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT),
                           ("LINEBEFORE", (0, 0), (0, -1), 3, color),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 7),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return t


def pipeline() -> Drawing:
    labels = ["Ticks MT5\n(2 ms)", "Régime de\nmarché", "Stratégie\n(Auto)",
              "Filtres\n+ IA", "Risque\npré-ordre", "Ordre MT5\n+ SL"]
    w, h, gap, y = 66, 44, 13, 8
    d = Drawing(170 * mm, 62)
    for i, lab in enumerate(labels):
        x = i * (w + gap)
        d.add(Rect(x, y, w, h, rx=6, ry=6, fillColor=NAVY if i not in (4,) else RED,
                   strokeColor=None))
        for j, line in enumerate(lab.split("\n")):
            d.add(String(x + w / 2, y + h - 17 - j * 12, line, fontName="Sans-Bold", fontSize=8.2,
                         fillColor=colors.white, textAnchor="middle"))
        if i < len(labels) - 1:
            x2 = x + w
            d.add(Line(x2 + 2, y + h / 2, x2 + gap - 3, y + h / 2, strokeColor=GOLD, strokeWidth=1.6))
            d.add(Polygon([x2 + gap - 3, y + h / 2 + 3.5, x2 + gap - 3, y + h / 2 - 3.5,
                           x2 + gap + 1, y + h / 2], fillColor=GOLD, strokeColor=None))
    return d


def on_page(c, doc) -> None:  # type: ignore[no-untyped-def]
    if doc.page == 1:
        return
    c.saveState()
    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(20 * mm, 285 * mm, 190 * mm, 285 * mm)
    c.setFont("Sans-Bold", 8.5)
    c.setFillColor(NAVY)
    c.drawString(20 * mm, 287 * mm, "ZEUS HFT — Guide d'utilisation")
    c.setFont("Sans", 8.5)
    c.setFillColor(GREY)
    c.drawRightString(190 * mm, 287 * mm, "v2.6")
    c.drawCentredString(105 * mm, 12 * mm, f"{doc.page}")
    c.restoreState()


def build(out: Path) -> None:
    s: list = []
    # ------------------------------------------------------------ couverture
    s += [Spacer(1, 55 * mm), P("ZEUS HFT", "title"), Spacer(1, 4 * mm),
          P("Guide d'utilisation — version 2.6", "subtitle"), Spacer(1, 3 * mm),
          P("Bot de trading automatique pour MetaTrader 5 · XAUUSD<br/>"
            "+ module de market making crypto (MEXC)", "subtitle"),
          Spacer(1, 30 * mm),
          callout("Ce guide explique comment installer le bot, le lancer automatiquement sur MT5, "
                  "comprendre ses décisions et le valider <b>avant</b> d'engager de l'argent réel. "
                  "Aucun robot ne garantit des gains : teste toujours sur un compte démo d'abord.",
                  title="À lire en premier"),
          PageBreak()]

    # ------------------------------------------------------------ sommaire
    s += [P("Sommaire", "h1"), table([
        ["#", "Chapitre", "Ce que tu y trouves"],
        ["1", "En bref", "Ce que fait le bot, en une page"],
        ["2", "Installation", "Prérequis MT5, fichiers, premier lancement"],
        ["3", "Démarrage automatique", "Lancement de MT5, du bot et de Windows, sans intervention"],
        ["4", "L'interface", "Chaque champ, chaque case, chaque bouton"],
        ["5", "Comment le bot décide", "Régimes de marché, stratégies, filtres"],
        ["6", "Capital et sorties", "Taille des lots, break-even, clôture partielle, trailing"],
        ["7", "Sécurités", "Kill-switch, pauses automatiques, reprise après crash"],
        ["8", "Calendrier des news", "Installer l'EA ZeusNewsExporter"],
        ["9", "Valider avant le réel", "Backtest, walk-forward, Monte Carlo, démo"],
        ["10", "Supervision", "Alertes Telegram, journal, réglages à chaud"],
        ["11", "Market making crypto", "Module MEXC (optionnel)"],
        ["12", "Dépannage", "Problèmes fréquents et solutions"],
        ["13", "Référence des commandes", "Toutes les commandes ZeusHFT.exe"],
    ], [10, 52, 108]), PageBreak()]

    # ------------------------------------------------------------ 1
    s += [P("1. En bref", "h1"),
          bullets(["<b>Ce qu'il fait</b> : lit le prix et le carnet d'ordres de MT5 toutes les 2 ms, "
                   "identifie le régime de marché, choisit une stratégie et place les ordres tout seul.",
                   "<b>Deux stratégies</b> : <i>momentum / cassure</i> en sessions actives (Londres, New York) "
                   "et <i>micro mean-reversion</i> en marché calme.",
                   "<b>Une seule position à la fois</b>, taille calculée pour risquer 0,5 % du capital. "
                   "Jamais de grille ni de martingale.",
                   "<b>Protégé</b> : SL de secours enregistré chez le broker, arrêt automatique à −3 % par jour, "
                   "pause après 4 pertes de suite, coupure si le flux se fige ou si MT5 se déconnecte.",
                   "<b>Automatique</b> : démarre avec Windows, lance MT5, se reconnecte, reprend le lendemain "
                   "d'un arrêt journalier."]),
          P("Les trois modes", "h2"),
          table([["Mode", "Flux de prix", "Ordres", "Usage"],
                 ["DRY_RUN", "MT5 réel", "Simulés", "Observer le bot sans risque"],
                 ["LIVE (compte démo)", "MT5 réel", "Réels (argent fictif)", "Le laisser apprendre et juger sa rentabilité"],
                 ["LIVE (compte réel)", "MT5 réel", "Réels", "Seulement après validation ; case « Autoriser compte réel » obligatoire"]],
                [38, 30, 36, 66]),
          Spacer(1, 6), P("Le pipeline de décision", "h2"), pipeline(),
          P("Chaque tick traverse ces étapes en environ 40 µs. Le bloc rouge (risque) peut refuser "
            "n'importe quel ordre.", "small"),
          PageBreak()]

    # ------------------------------------------------------------ 2
    s += [P("2. Installation", "h1"), P("Prérequis", "h2"),
          bullets(["Windows 10 ou 11, MetaTrader 5 installé, un compte (démo pour commencer).",
                   "Dans MT5 : <b>Outils → Options → Expert Advisors → Autoriser le trading algorithmique</b>.",
                   "Le bouton <b>« Algo Trading »</b> de la barre d'outils de MT5 doit être <b>vert</b>.",
                   "Le symbole (XAUUSD ou son équivalent chez ton broker) doit être visible dans le Market Watch."]),
          P("Option 1 — l'application (recommandé)", "h2"),
          steps(["Télécharge <b>ZeusHFT-windows.zip</b> depuis la page <i>Releases</i> du dépôt GitHub "
                 "<b>ck526438-lgtm/zeus</b>.",
                 "Décompresse-le dans un dossier fixe, par exemple <b>C:\\ZeusHFT</b>. Le bot y crée un "
                 "sous-dossier <b>data</b> pour sa mémoire et ses journaux.",
                 "Lance <b>ZeusHFT-App.exe</b> (interface) ou <b>ZeusHFT.exe</b> (ligne de commande)."]),
          P("Option 2 — depuis le code source", "h2"),
          code("git clone https://github.com/ck526438-lgtm/zeus\ncd zeus\ninstall.bat\nstart_zeus_hft.bat"),
          P("Identifiants (facultatif)", "h2"),
          P("Sans identifiants, le bot utilise le compte déjà ouvert dans MT5. Pour qu'il se connecte seul, "
            "copie <b>.env.example</b> en <b>.env</b> et renseigne :"),
          code("ZEUS_MT5_LOGIN=12345678\nZEUS_MT5_PASSWORD=ton_mot_de_passe\nZEUS_MT5_SERVER=NomDuBroker-Demo"),
          callout("Le fichier <b>.env</b> contient tes mots de passe : ne le partage jamais et ne le mets "
                  "jamais sur GitHub.", RED),
          PageBreak()]

    # ------------------------------------------------------------ 3
    s += [P("3. Démarrage automatique", "h1"),
          P("Objectif : allumer le PC (ou le VPS) et laisser le bot trader sans rien toucher."),
          P("Mise en place (une seule fois)", "h2"),
          steps(["Ouvre <b>ZeusHFT-App.exe</b>, règle le mode (<b>LIVE</b> sur un compte démo), la stratégie "
                 "(<b>Auto</b>), le risque par trade et la perte max par jour.",
                 "Coche <b>« Démarrage automatique »</b>.",
                 "Clique sur <b>« Lancer avec Windows »</b> : l'appli s'ouvrira à chaque démarrage du PC.",
                 "Facultatif : renseigne le <b>jeton</b> et le <b>chat id</b> Telegram pour recevoir les alertes.",
                 "Ferme l'appli et redémarre le PC pour vérifier."]),
          P("Ce qui se passe ensuite, tout seul", "h2"),
          table([["Étape", "Ce que fait le bot"],
                 ["1. Ouverture", "L'appli démarre avec Windows et attend 3 s."],
                 ["2. MT5", "Le bot trouve <b>terminal64.exe</b> et lance MetaTrader 5 s'il n'est pas ouvert, "
                            "puis se connecte (avec les identifiants du .env s'ils sont fournis)."],
                 ["3. Symbole", "Il trouve le bon nom chez ton broker : XAUUSD, XAUUSDm, XAUUSD.r, GOLD…"],
                 ["4. Historique", "Il pré-charge les bougies M1, M15 et H1 : il peut trader dès la première minute."],
                 ["5. Autorisation", "Il vérifie le bouton « Algo Trading » et les droits du compte. S'ils manquent, il "
                                     "suspend les ordres, t'alerte, puis reprend tout seul dès que c'est corrigé."],
                 ["6. Trading", "Il trade selon les sessions et les régimes. La nuit et le week-end, il attend."],
                 ["7. Incidents", "MT5 pas prêt : nouvel essai toutes les 30 s. Flux figé : pause, puis reprise. "
                                  "Perte max du jour : arrêt, puis reprise le lendemain à 00:00 UTC."]],
                [32, 138]),
          Spacer(1, 6),
          callout("Pour trader 24 h/24 sans dépendre de ton PC : loue un <b>VPS Windows</b> situé près du "
                  "serveur de ton broker. La commande <b>ZeusHFT.exe probe</b> mesure la latence.",
                  title="Recommandation"),
          PageBreak()]

    # ------------------------------------------------------------ 4
    s += [P("4. L'interface", "h1"), P("Réglages", "h2"),
          table([["Champ", "Rôle", "Conseil"],
                 ["Mode", "DRY_RUN (simulation) ou LIVE (ordres réels)", "LIVE sur compte démo pour apprendre"],
                 ["Stratégie", "Auto, Momentum, Mean-reversion, OBI + IA", "Auto"],
                 ["Risque/trade %", "Part du capital risquée par trade (taille des lots calculée)", "0,25 à 0,5 %"],
                 ["Lot si 0 %", "Lot fixe si le risque est à 0", "0,01"],
                 ["Perte max/jour", "Arrêt automatique jusqu'au lendemain", "1 à 3 % du capital"],
                 ["Spread max (pts)", "Aucune entrée au-delà", "Un peu au-dessus de ton spread habituel"],
                 ["News (UTC)", "Pause 15 min avant / après. Ex. 13:30 ou 2026-12-04 13:30", "Utilise plutôt l'EA (chap. 8)"],
                 ["Filtre tendance H1/M15", "Interdit les trades contre la tendance de fond", "Toujours coché"],
                 ["IA adaptative", "Apprend en continu et peut bloquer une entrée (veto)", "Coché"],
                 ["Simulation sans MT5", "Flux synthétique, pour tester l'interface", "Décoché en vrai"],
                 ["Autoriser compte réel", "Sans cette case, le bot refuse un compte réel ; jamais mémorisée", "Décoché au début"],
                 ["Démarrage automatique", "Démarre à l'ouverture et se relance si MT5 n'est pas prêt", "Coché"],
                 ["Lancer avec Windows", "Ouvre l'appli au démarrage du PC", "Activé sur un VPS"]],
                [38, 82, 50]),
          P("Boutons", "h2"),
          bullets(["<b>Démarrer</b> : lance le bot avec les réglages affichés.",
                   "<b>Arrêter (ferme tout)</b> : annule les ordres, ferme la position, sauvegarde la mémoire de l'IA.",
                   "<b>ARRÊT D'URGENCE</b> : kill-switch immédiat ; seules les fermetures restent permises."]),
          PageBreak(), P("Tableau de bord", "h2"),
          table([["Carte", "Lecture"],
                 ["Régime de marché", "TENDANCE, NORMAL, CALME = trading possible · OUVERTURE, ROLLOVER, NEWS, CHOC, HORS-SESSION = attente"],
                 ["Tendance H1 / M15 · ADX", "↑ hausse, ↓ baisse, — neutre ; ADX > 22 = marché en tendance"],
                 ["Stratégie en position", "Stratégie qui gère le trade en cours"],
                 ["ATR M1 / vol×", "Volatilité en points ; vol× > 2,5 = marché agité, momentum désactivé"],
                 ["IA : précision / poids", "Le poids reste à 0 tant que la précision ne dépasse pas 50 %"],
                 ["Risque", "OK, PAUSE (réduction seule) ou KILL"],
                 ["Profit factor (100 derniers)", "Au-dessus de 1,2 et en gain net : la démo devient encourageante"],
                 ["Barre du bas", "Décision en cours, prochaine news, verdict de rentabilité, résultat par stratégie"]],
                [52, 118]),
          PageBreak()]

    # ------------------------------------------------------------ 5
    s += [P("5. Comment le bot décide", "h1"), P("Régimes de marché (mode Auto)", "h2"),
          table([["Régime", "Quand (heure UTC)", "Stratégie"],
                 ["TENDANCE / NORMAL", "08:00–11:30 et 13:30–17:00, volatilité 0,6 à 2,5 × la normale", "Momentum / cassure"],
                 ["CALME", "19:30–21:00 et 23:30–06:00, volatilité ≤ 1,1 × la normale, ADX M15 ≤ 22", "Mean-reversion"],
                 ["OUVERTURE", "5 premières minutes de Londres et de New York", "Aucune"],
                 ["ROLLOVER", "21:00–23:00 (les spreads explosent)", "Aucune"],
                 ["NEWS", "15 min avant à 15 min après une annonce à fort impact", "Aucune"],
                 ["CHOC", "Le prix bouge de plus de 2 ATR en 2 s", "Aucune pendant 2 min"]],
                [36, 92, 42]),
          P("Stratégie 1 — Momentum / cassure", "h2"),
          bullets(["<b>Entrée</b> : le prix casse le plus haut (ou le plus bas) des 60 dernières secondes "
                   "<i>et</i> accélère de plus de 0,45 ATR en 2 s, dans le sens de la tendance H1. Ordre au marché.",
                   "<b>Sorties</b> : TP1 à 0,8 ATR, SL à 0,5 ATR, durée max 60 s, sortie si la vitesse se retourne."]),
          P("Stratégie 2 — Micro mean-reversion", "h2"),
          bullets(["<b>Entrée</b> : le prix s'écarte d'au moins 1,2 ATR de la moyenne courte, puis ralentit. "
                   "Ordre limite, donc pas de spread payé à l'entrée.",
                   "<b>Sorties</b> : retour vers la moyenne, SL 1,2 ATR. Sortie immédiate si la volatilité monte : "
                   "sur l'or, une extension peut vite devenir une tendance."]),
          P("Filtres communs (toutes les conditions doivent être vraies)", "h2"),
          bullets(["Gain visé ≥ <b>2,5 × le spread</b>, sinon le coût mange le gain.",
                   "Spread actuel ≤ <b>1,5 × sa moyenne</b> et ≤ ton plafond.",
                   "Pas de trade contre la tendance H1 (EMA 50/200).",
                   "Pas de veto de l'IA (si elle est confiante dans le sens opposé).",
                   "Pause de 20 s après chaque sortie."]),
          PageBreak()]

    # ------------------------------------------------------------ 6
    s += [P("6. Capital et sorties", "h1"), P("Taille des lots", "h2"),
          code("lots = capital × risque % / (distance du SL en points × valeur d'un point pour 1 lot)"),
          bullets(["Exemple : 10 000 $, 0,5 % = 50 $ ; SL à 100 points ; 1 $ par point pour 1 lot → <b>0,50 lot</b>.",
                   "Arrondi au pas inférieur : le bot ne risque jamais plus que prévu.",
                   "Plafonné par <b>max_lots</b> et par l'exposition max (20 × le capital).",
                   "Si même le lot minimum risque plus de 1,5 × ton budget, le trade est refusé."]),
          P("Gestion active du trade", "h2"),
          table([["Moment", "Action"],
                 ["Entrée", "SL de la stratégie (1R) + <b>SL de secours chez le broker</b> à 1,5 × cette distance"],
                 ["+1R", "<b>Break-even</b> : le SL passe au prix d'entrée + commission (le trade ne coûte plus rien)"],
                 ["Premier objectif", "<b>Clôture partielle de 50 %</b> (ou totale si la taille ne peut pas être divisée)"],
                 ["Reste de la position", "<b>Trailing stop</b> à 1 ATR du meilleur prix atteint"],
                 ["Chaque resserrement", "Le SL côté serveur MT5 est mis à jour (il ne fait que se resserrer)"]],
                [40, 130]),
          Spacer(1, 6),
          callout("Avec 0,01 lot, la clôture partielle est impossible : la position est fermée en entier au "
                  "premier objectif. La clôture partielle devient active à partir de 0,02 lot.", GOLD),
          PageBreak()]

    # ------------------------------------------------------------ 7
    s += [P("7. Sécurités", "h1"),
          table([["Protection", "Déclencheur", "Effet"],
                 ["Perte du jour", "3 % du capital (ou montant saisi)", "Arrêt, fermeture, reprise à 00:00 UTC"],
                 ["Série de pertes", "4 pertes de suite", "Pause de 2 h (réduction seule)"],
                 ["Drawdown glissant", "1,5 % en moins de 60 min", "Réduction seule pendant 1 h"],
                 ["Exposition", "Plus de 20 × le capital", "Taille réduite ou ordre refusé"],
                 ["Spread", "Au-dessus du plafond ou de 1,5 × la moyenne", "Pas d'entrée"],
                 ["Latence", "Plus de 150 ms", "Entrées bloquées ; arrêt si 3 fois de suite"],
                 ["Rejets", "Plus de 30 % sur 60 s, ou 5 de suite", "Mode dégradé 5 min, puis arrêt"],
                 ["Flux figé", "Aucun tick depuis 30 s", "Ordres annulés, pause, reprise automatique"],
                 ["Algo Trading", "Bouton MT5 désactivé ou compte en lecture seule", "Ordres suspendus + alerte"],
                 ["Compte réel", "Case non cochée", "Démarrage refusé"],
                 ["Crash / redémarrage", "—", "État restauré : un arrêt du jour reste actif"]],
                [36, 66, 68]),
          P("Réduction seule", "h2"),
          P("En pause, le bot n'ouvre plus rien, mais il peut toujours <b>fermer</b> ou réduire une position. "
            "Une position ouverte reste protégée par son SL serveur, même si le PC s'éteint."),
          PageBreak()]

    # ------------------------------------------------------------ 8
    s += [P("8. Calendrier des news (EA ZeusNewsExporter)", "h1"),
          P("Le paquet Python de MT5 ne donne pas accès au calendrier économique. Le petit EA fourni l'exporte "
            "pour le bot. Il ne passe <b>aucun ordre</b>."),
          steps(["Dans MT5 : <b>Fichier → Ouvrir le dossier des données</b>, puis <b>MQL5\\Experts</b>.",
                 "Copie-y <b>ZeusNewsExporter.mq5</b> (dans le zip).",
                 "Ouvre-le dans MetaEditor (F4) et compile-le (F7). Il ne doit y avoir aucune erreur.",
                 "Dans MT5, fais glisser <b>ZeusNewsExporter</b> sur n'importe quel graphique, puis valide.",
                 "L'onglet <i>Experts</i> affiche « Zeus : N annonces exportées ». La prochaine news apparaît en "
                 "bas de l'appli."]),
          bullets(["Le calendrier est mis à jour toutes les 15 min ; il couvre les annonces USD à fort impact "
                   "(NFP, CPI, FOMC, Fed…).",
                   "Le bot fait une pause de 15 min avant à 15 min après chaque annonce.",
                   "Les paramètres de l'EA permettent d'ajouter d'autres devises (ex. USD,EUR) et l'impact moyen."]),
          PageBreak()]

    # ------------------------------------------------------------ 9
    s += [P("9. Valider avant le réel", "h1"),
          callout("Ordre obligatoire : backtest sur tes vrais ticks → walk-forward → Monte Carlo → "
                  "2 à 4 semaines en LIVE démo → réel en lot minimum.", RED, "Règle d'or"),
          P("Étape 1 — Télécharger tes données", "h2"),
          code("ZeusHFT.exe download --symbol XAUUSD --days 60"),
          P("Ticks + bougies M15/H1 de <b>ton</b> broker (spread et horaires réels).", "small"),
          P("Étape 2 — Backtest tick par tick", "h2"),
          code("ZeusHFT.exe backtest --csv data/XAUUSD_ticks.csv --utc-offset 2"),
          P("--utc-offset = décalage de l'heure du serveur MT5 par rapport à UTC (souvent 2 ou 3).", "small"),
          P("Étape 3 — Walk-forward (le test anti-illusion)", "h2"),
          code("ZeusHFT.exe walkforward --csv data/XAUUSD_ticks.csv --train-days 20 --test-days 5"),
          bullets(["Optimise sur 20 jours, teste <b>sans rien changer</b> sur les 5 suivants, puis décale la fenêtre.",
                   "Seul le résultat <b>hors échantillon (OOS)</b> compte. Une efficacité inférieure à 50 % ou une "
                   "majorité de fenêtres perdantes signalent une sur-optimisation : <b>ne passe pas en réel</b>."]),
          P("Étape 4 — Monte Carlo", "h2"),
          code("ZeusHFT.exe backtest --csv data/XAUUSD_ticks.csv --montecarlo 5000 --mc-cost 0.5\n"
               "ZeusHFT.exe montecarlo --trades data/zeus_hft_trades.csv --mode LIVE"),
          bullets(["Mélange l'ordre des trades, en saute 5 %, ajoute du slippage aléatoire.",
                   "<b>ROBUSTE</b> = encore positif au 5<super>e</super> percentile. En dessous de 30 trades, aucune conclusion."]),
          P("Étape 5 — LIVE sur compte démo", "h2"),
          bullets(["Laisse tourner 2 à 4 semaines. Lis le <b>verdict</b> en bas de l'appli.",
                   "Critère minimal avant le réel : <b>« RENTABLE »</b> sur 100 trades ou plus (PF ≥ 1,2, gain net), "
                   "<b>et</b> walk-forward positif."]),
          PageBreak()]

    # ------------------------------------------------------------ 10
    s += [P("10. Supervision", "h1"), P("Alertes Telegram", "h2"),
          steps(["Sur Telegram, écris à <b>@BotFather</b> → /newbot → copie le <b>jeton</b>.",
                 "Écris à <b>@userinfobot</b> pour obtenir ton <b>chat id</b>.",
                 "Colle les deux dans l'appli (ligne « Alertes Telegram »), puis envoie un message à ton bot."]),
          P("Tu recevras : démarrage/arrêt, kill-switch, pauses, MT5 déconnecté, latence, Algo Trading coupé, "
            "bilan du jour.", "small"),
          P("Fichiers du dossier data", "h2"),
          table([["Fichier", "Contenu"],
                 ["zeus_hft_trades.csv", "Chaque trade : stratégie, prix, durée, PnL, frais, spread, slippage, latence (s'ouvre dans Excel)"],
                 ["zeus_hft_model_XAUUSD.json", "Mémoire de l'IA (sauvegardée chaque minute)"],
                 ["zeus_hft_state_*.json", "État du risque (reprise après crash)"],
                 ["ticks/*.csv.gz", "Chaque tick reçu, rejouable en backtest"],
                 ["zeus_hft.log", "Journal technique"],
                 ["zeus_hft_hot.json", "Réglages à chaud (à créer si besoin)"]],
                [55, 115]),
          P("Réglages à chaud (sans redémarrer)", "h2"),
          P("Crée ou modifie <b>data/zeus_hft_hot.json</b>. Le bot l'applique en 5 s :"),
          code('{"trading_enabled": false}            ← pause manuelle\n'
               '{"risk_per_trade_pct": 0.25, "max_spread_points": 30}'),
          P("Journal infalsifiable", "h2"),
          code("ZeusHFT.exe verify-journal"),
          P("Chaque ligne du journal est liée à la précédente par une empreinte SHA-256 : toute modification "
            "après coup est détectée.", "small"),
          P("Prometheus / Grafana (avancé)", "h2"),
          code("ZeusHFT.exe run --mode LIVE --metrics-port 9108   →  http://127.0.0.1:9108/metrics"),
          PageBreak()]

    # ------------------------------------------------------------ 11
    s += [P("11. Market making crypto (MEXC, optionnel)", "h1"),
          bullets(["<b>Principe</b> : poser en permanence un ordre d'achat et un ordre de vente, et capturer l'écart.",
                   "<b>Pourquoi MEXC</b> : 0 % de frais maker sur de nombreux perpétuels. À 0,02 % (Binance standard), "
                   "les frais dépassent le spread capturé.",
                   "<b>Protections</b> : retrait automatique en cas de flux agressif, de balayage du carnet, de "
                   "<b>markout</b> négatif (on se fait « sniper »), d'inventaire maximal ou de carnet désynchronisé."]),
          P("Mise en route", "h2"),
          code("ZeusHFT.exe probe                         latence vers les exchanges\n"
               "ZeusHFT.exe mm-scan                       meilleures paires à 0 % maker\n"
               "ZeusHFT.exe mm-run --symbol SOL_USDT      DRY_RUN : vrai flux, ordres simulés\n"
               'ZeusHFT.exe mm-backtest --file "data/mm/SOL_USDT_*.jsonl.gz"\n'
               "ZeusHFT.exe mm-run --symbol SOL_USDT --mode LIVE --confirm-live --size 1"),
          bullets(["Clés API dans .env : <b>ZEUS_MM_API_KEY</b> et <b>ZEUS_MM_API_SECRET</b>. "
                   "Droits de <b>trading uniquement</b>, jamais de retrait ; restreins la clé à l'IP de ton VPS.",
                   "Le KPI n°1 est le <b>markout moyen</b> : s'il est négatif, le spread ne compense pas la "
                   "sélection adverse. Ne passe en LIVE qu'avec un markout positif sur 100 exécutions ou plus.",
                   "Ce module n'a pas encore été testé contre le vrai MEXC : commence par DRY_RUN, puis 1 contrat."]),
          PageBreak()]

    # ------------------------------------------------------------ 12
    s += [P("12. Dépannage", "h1"),
          table([["Symptôme", "Cause probable", "Solution"],
                 ["« Connexion MT5 impossible »", "MT5 pas installé, ou installé à un endroit inhabituel",
                  "Renseigne ZEUS_MT5_PATH=C:\\...\\terminal64.exe dans .env"],
                 ["« Algo Trading désactivé »", "Bouton de la barre d'outils MT5 en rouge", "Clique dessus (vert) : reprise automatique"],
                 ["« symbole introuvable »", "Nom différent chez le broker", "Ajoute-le au Market Watch ou saisis le nom exact"],
                 ["Aucun trade", "Hors session, spread trop élevé, tendance H1 contraire, news", "Lis la barre « Décision » : elle donne la raison"],
                 ["Régime = HORS-SESSION toute la journée", "Mauvais décalage horaire", "Vérifie l'heure du PC (le bot détecte le décalage tout seul)"],
                 ["« ordre rejeté 10030 »", "Mode de remplissage refusé par le broker", "Corrigé automatiquement au 2<super>e</super> essai"],
                 ["Pause « flux figé »", "Marché fermé (week-end) ou MT5 déconnecté", "Rien à faire : reprise automatique"],
                 ["KILL après redémarrage", "Perte max du jour déjà atteinte", "Normal : reprise le lendemain à 00:00 UTC"],
                 ["Lots plus gros que prévu", "Risque % appliqué au capital du compte", "Baisse « Risque/trade » ou mets 0 % (lot fixe)"],
                 ["Calendrier introuvable", "EA ZeusNewsExporter non lancé", "Voir chapitre 8"]],
                [44, 58, 68]),
          PageBreak()]

    # ------------------------------------------------------------ 13
    s += [P("13. Référence des commandes", "h1"),
          table([["Commande", "Rôle"],
                 ["ZeusHFT.exe gui", "Interface (identique à ZeusHFT-App.exe)"],
                 ["ZeusHFT.exe run --mode LIVE", "Trading réel (compte démo conseillé)"],
                 ["ZeusHFT.exe run", "DRY_RUN sur le flux MT5 réel"],
                 ["ZeusHFT.exe run --synthetic", "Démo sans MT5"],
                 ["  --strategy auto|momentum|meanrev|obi", "Choix de la stratégie"],
                 ["  --news \"13:30, 2026-12-04 13:30\"", "Heures de news manuelles (UTC)"],
                 ["  --max-spread 35 · --lot 0.01 · --max-dd 300", "Spread, lot fixe, perte max du jour"],
                 ["  --confirm-live", "Autorise un compte réel"],
                 ["ZeusHFT.exe download --days 60", "Ticks + historique M15/H1 de ton broker"],
                 ["ZeusHFT.exe backtest --csv … --utc-offset 2", "Backtest tick par tick"],
                 ["  --montecarlo 5000 --mc-cost 0.5", "Monte Carlo sur les trades du backtest"],
                 ["ZeusHFT.exe walkforward --csv …", "Optimisation glissante + test hors échantillon"],
                 ["ZeusHFT.exe montecarlo --trades …", "Robustesse des trades réels (journal)"],
                 ["ZeusHFT.exe verify-journal", "Vérifie que le journal n'a pas été modifié"],
                 ["ZeusHFT.exe probe", "Latence réseau : P50 / P99 / P99.9 / gigue"],
                 ["ZeusHFT.exe mm-scan · mm-run · mm-backtest", "Market making MEXC"]],
                [80, 90]),
          Spacer(1, 10),
          callout("Le trading avec effet de levier peut faire perdre plus que le capital investi. Les "
                  "résultats passés, de backtest ou de démo, ne garantissent pas les résultats futurs. "
                  "Ce logiciel est fourni sans garantie (licence MIT). Ce guide n'est pas un conseil en investissement.",
                  RED, "Avertissement")]

    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm, title="Guide Zeus HFT v2.6",
                            author="Zeus HFT")
    doc.build(s, onFirstPage=on_page, onLaterPages=on_page)


if __name__ == "__main__":
    out = Path(__file__).with_name("Guide_Zeus_HFT.pdf")
    build(out)
    print(out)
