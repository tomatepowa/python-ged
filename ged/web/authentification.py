"""Authentification Windows intégrée : le navigateur s'identifie avec la session Windows ouverte.

Le serveur répond 401 avec « WWW-Authenticate: NTLM » (ou « Negotiate », Kerberos) ; Edge et
Chrome renvoient alors un jeton de la session Windows, sans rien demander, pour les sites de la
zone « Intranet local ». Le jeton est vérifié par Windows (SSPI) auprès du contrôleur de domaine,
qui fournit aussi la liste complète des groupes de la personne, groupes imbriqués compris.

Une fois la personne identifiée, un cookie de session évite de renégocier à chaque requête.
Les sessions sont gardées en mémoire : un redémarrage du serveur les oublie, et le navigateur
se réidentifie sans que la personne ne s'en aperçoive.

NTLM se négocie en deux allers-retours sur la même connexion TCP : la négociation en cours est
donc rattachée à la connexion (adresse et port du client).

Kerberos (« Negotiate ») ne fonctionne que si le compte qui fait tourner le serveur porte le SPN
HTTP/<nom du serveur> (service lancé sous le compte machine, ou SPN déclaré par un administrateur
du domaine). Sinon, garder NTLM.
"""

from __future__ import annotations

import base64
import binascii
import logging
import secrets
import sys
import time
from dataclasses import dataclass
from typing import Protocol

from ged.acces import Lecteur

log = logging.getLogger(__name__)

COOKIE = "ged_session"
# Ce navigateur a déjà su s'identifier : on négocie directement, sans page d'accueil
COOKIE_NAVIGATEUR = "ged_navigateur"
# Une négociation NTLM abandonnée (onglet fermé entre les deux allers-retours) est oubliée après
DUREE_NEGOCIATION_S = 60

# Attributs des groupes d'un jeton Windows (winnt.h)
SE_GROUP_ENABLED = 0x4
SE_GROUP_USE_FOR_DENY_ONLY = 0x10


def identification_sur_demande(agent: str) -> bool:
    """Navigateur qui n'envoie la session Windows qu'aux sites qu'on lui a explicitement indiqués
    (Firefox : network.automatic-ntlm-auth.trusted-uris) et demande sinon un mot de passe."""
    return "firefox/" in agent.lower()


class AuthentificationRefusee(Exception):
    """Jeton invalide, mot de passe faux, compte désactivé..."""


class Negociateur(Protocol):
    """Vérifie les jetons du navigateur (SSPI sous Windows, factice dans les tests)."""

    def avancer(self, connexion: tuple, jeton: bytes) -> tuple[bytes | None, Lecteur | None]:
        """Étape suivante de la négociation sur cette connexion.

        Renvoie (jeton à renvoyer au navigateur, personne identifiée si la négociation est
        terminée). Lève AuthentificationRefusee si le jeton est refusé.
        """
        ...


class NegociateurSSPI:
    """Négociation par l'API de sécurité de Windows (pywin32)."""

    def __init__(self, protocole: str, sids_admin: frozenset[str]):
        if sys.platform != "win32":
            raise SystemExit("L'authentification Windows ne fonctionne que sous Windows "
                             "([authentification] active = false pour s'en passer).")
        self.protocole = protocole
        self.sids_admin = sids_admin
        # connexion -> (contexte SSPI, heure de début)
        self.en_cours: dict[tuple, tuple[object, float]] = {}

    def avancer(self, connexion: tuple, jeton: bytes) -> tuple[bytes | None, Lecteur | None]:
        import pywintypes
        import sspi
        import sspicon

        maintenant = time.monotonic()
        for cle, (_, debut) in list(self.en_cours.items()):
            if maintenant - debut > DUREE_NEGOCIATION_S:
                del self.en_cours[cle]
        contexte, debut = self.en_cours.pop(connexion, (None, maintenant))
        if contexte is None:
            contexte = sspi.ServerAuth(self.protocole)
        try:
            code, tampons = contexte.authorize(jeton)
        except pywintypes.error as exc:
            raise AuthentificationRefusee(exc.strerror) from exc
        sortant = tampons[0].Buffer or None
        if code in (sspicon.SEC_I_CONTINUE_NEEDED, sspicon.SEC_I_COMPLETE_AND_CONTINUE):
            self.en_cours[connexion] = (contexte, debut)
            return sortant, None
        return sortant, self._lecteur(contexte)

    def _lecteur(self, contexte) -> Lecteur:
        """Compte et groupes de la personne, lus dans le jeton Windows de la négociation."""
        import sspicon
        import win32security

        compte = contexte.ctxt.QueryContextAttributes(sspicon.SECPKG_ATTR_NAMES)
        jeton = contexte.ctxt.QuerySecurityContextToken()
        try:
            utilisateur = win32security.ConvertSidToStringSid(
                win32security.GetTokenInformation(jeton, win32security.TokenUser)[0]
            )
            groupes = win32security.GetTokenInformation(jeton, win32security.TokenGroups)
        finally:
            jeton.Close()
        actifs, tous = {utilisateur}, {utilisateur}
        for sid, attributs in groupes:
            texte = win32security.ConvertSidToStringSid(sid)
            tous.add(texte)
            if attributs & SE_GROUP_ENABLED and not attributs & SE_GROUP_USE_FOR_DENY_ONLY:
                actifs.add(texte)
        return Lecteur(
            compte=compte,
            sids=frozenset(actifs),
            sids_refus=frozenset(tous),
            admin=bool(self.sids_admin & actifs),
            sid=utilisateur,
        )


def sids_des_comptes(noms: list[str]) -> frozenset[str]:
    """SID des groupes (ou comptes) « DOMAINE\\groupe » ; un SID (S-1-...) est gardé tel quel."""
    sids = set()
    for nom in noms:
        if nom.upper().startswith("S-1-"):
            sids.add(nom.upper())
            continue
        import pywintypes
        import win32security

        try:
            sid, _, _ = win32security.LookupAccountName(None, nom)
        except pywintypes.error as exc:
            log.warning("Groupe d'administration introuvable : %s (%s)", nom, exc.strerror)
            continue
        sids.add(win32security.ConvertSidToStringSid(sid))
    return frozenset(sids)


@dataclass
class Session:
    lecteur: Lecteur
    expire: float


class Authentification:
    """Sessions et étapes de la négociation, indépendamment du serveur web."""

    def __init__(self, protocole: str, negociateur: Negociateur, duree_session_s: float):
        self.protocole = protocole
        self.negociateur = negociateur
        self.duree_session_s = duree_session_s
        self.sessions: dict[str, Session] = {}

    def lecteur_de_session(self, cookie: str | None) -> Lecteur | None:
        session = self.sessions.get(cookie or "")
        if session is None:
            return None
        if session.expire < time.monotonic():
            del self.sessions[cookie]
            return None
        return session.lecteur

    def ouvrir_session(self, lecteur: Lecteur) -> str:
        maintenant = time.monotonic()
        for cle in [c for c, s in self.sessions.items() if s.expire < maintenant]:
            del self.sessions[cle]
        cookie = secrets.token_urlsafe(32)
        self.sessions[cookie] = Session(lecteur, maintenant + self.duree_session_s)
        return cookie

    def jeton_recu(self, entete: str) -> bytes | None:
        """Jeton de l'en-tête Authorization, s'il est au format attendu (« NTLM <base64> »)."""
        schema, _, donnees = entete.strip().partition(" ")
        if schema.lower() != self.protocole.lower() or not donnees:
            return None
        try:
            return base64.b64decode(donnees, validate=True)
        except binascii.Error:
            return None

    def entete_reponse(self, jeton: bytes | None = None) -> str:
        """Valeur de WWW-Authenticate : le protocole seul, ou suivi du jeton à renvoyer."""
        return f"{self.protocole} {base64.b64encode(jeton).decode()}" if jeton else self.protocole
