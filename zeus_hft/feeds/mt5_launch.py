"""Démarrage automatique de MetaTrader 5 : trouver le terminal, résoudre le symbole du
broker, vérifier que le trading algorithmique est autorisé.

- ``find_terminal`` : cherche ``terminal64.exe`` dans les dossiers d'installation habituels
  (MetaTrader 5 « officiel » ou version du broker). ``mt5.initialize(path=...)`` lance
  alors le terminal tout seul s'il n'est pas ouvert.
- ``resolve_symbol`` : XAUUSD s'appelle selon le broker XAUUSD, XAUUSDm, XAUUSD.r, GOLD…
- ``trading_permission`` : le bouton « Algo Trading » de MT5 et les droits du compte.
"""
from __future__ import annotations

import glob
import os
from typing import Any, Callable, Iterable

ALIASES = {"XAUUSD": ("GOLD", "XAUUSD"), "XAGUSD": ("SILVER", "XAGUSD"),
           "US30": ("DJ30", "WS30", "DOW", "US30"), "NAS100": ("USTEC", "NDX100", "US100", "NAS100")}


def candidate_dirs() -> list[str]:
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
             os.environ.get("APPDATA", "")]
    return [r for r in roots if r]


def find_terminal(roots: Iterable[str] | None = None,
                  globber: Callable[[str], list[str]] = glob.glob,
                  mtime: Callable[[str], float] = os.path.getmtime) -> str:
    """Chemin de terminal64.exe le plus récemment utilisé, ou '' si introuvable."""
    found: list[str] = []
    for root in roots if roots is not None else candidate_dirs():
        found += globber(os.path.join(root, "*", "terminal64.exe"))
        found += globber(os.path.join(root, "MetaQuotes", "*", "terminal64.exe"))
    if not found:
        return ""

    def key(p: str) -> tuple[int, float]:
        official = 1 if "metatrader 5" in p.lower() else 0
        try:
            t = mtime(p)
        except OSError:
            t = 0.0
        return (official, t)

    return sorted(set(found), key=key, reverse=True)[0]


def resolve_symbol(wanted: str, available: Iterable[str]) -> str | None:
    """Nom exact chez le broker, ou meilleur candidat (suffixes .r, m, -ECN, alias GOLD…)."""
    names = list(available)
    if wanted in names:
        return wanted
    low = {n.lower(): n for n in names}
    if wanted.lower() in low:
        return low[wanted.lower()]
    roots = ALIASES.get(wanted.upper(), (wanted.upper(),))
    cands = [n for n in names if any(n.upper().startswith(r) for r in roots)]
    if not cands:
        return None
    # le plus court = le moins de suffixes exotiques (XAUUSD.r avant XAUUSD.r.micro)
    return sorted(cands, key=lambda n: (len(n), n))[0]


def trading_permission(terminal: Any, account: Any) -> tuple[bool, str]:
    if terminal is None or account is None:
        return False, "terminal MT5 non connecté"
    if not getattr(terminal, "trade_allowed", True):
        return False, "« Algo Trading » désactivé dans MT5 (bouton de la barre d'outils)"
    if not getattr(account, "trade_allowed", True):
        return False, "le compte n'autorise pas le trading (mot de passe investisseur ?)"
    if not getattr(account, "trade_expert", True):
        return False, "le broker interdit le trading automatique sur ce compte"
    return True, ""
