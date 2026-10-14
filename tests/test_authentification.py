"""Identification Windows dans l'interface web (sans base de données : seules des pages qui
répondent avant toute requête SQL sont appelées)."""

from __future__ import annotations

import base64
import sys

import pytest
from fastapi.testclient import TestClient

from ged.acces import Lecteur
from ged.config import Config
from ged.web.app import creer_app
from ged.web.authentification import COOKIE, AuthentificationRefusee

TECH = Lecteur("CLINIQUE\\tech", frozenset({"S-1-5-21-1-2-3-1101"}), frozenset({"S-1-5-21-1-2-3-1101"}))


class NegociateurFactice:
    """Négociation NTLM en deux temps, comme le vrai : défi, puis personne identifiée."""

    def __init__(self):
        self.connexions = []

    def avancer(self, connexion, jeton):
        self.connexions.append(connexion)
        if jeton == b"negociation":
            return b"defi", None
        if jeton == b"reponse":
            return None, TECH
        raise AuthentificationRefusee("Mot de passe incorrect")


def ntlm(jeton: bytes) -> dict:
    return {"Authorization": "NTLM " + base64.b64encode(jeton).decode()}


@pytest.fixture
def client():
    # Base injoignable : le pool de connexions n'est pas ouvert (pas de « with TestClient »)
    app = creer_app(Config(url_base="postgresql://ged@127.0.0.1:1/ged", sources=[]), NegociateurFactice())
    return TestClient(app)


def test_sans_identification_le_navigateur_est_invite_a_s_identifier(client):
    reponse = client.get("/")
    assert reponse.status_code == 401
    assert reponse.headers["WWW-Authenticate"] == "NTLM"
    assert "Identification Windows" in reponse.text


@pytest.mark.parametrize(
    "adresse, conseil",
    [
        # Une adresse à points est en zone Internet : le navigateur demande un mot de passe
        ("http://127.0.0.1:8000/", "http://localhost:8000/"),
        ("http://vm-ged.cliniques.local:8000/", "nom court du serveur"),
        ("http://vm-ged:8000/", None),
    ],
)
def test_page_d_identification_explique_les_adresses_a_points(client, adresse, conseil):
    texte = client.get(adresse).text
    if conseil:
        assert conseil in texte
    else:
        assert "contient des points" not in texte


def test_fichiers_statiques_sans_identification(client):
    assert client.get("/static/style.css").status_code == 200


def test_negociation_puis_session(client):
    defi = client.get("/stats", headers=ntlm(b"negociation"))
    assert defi.status_code == 401
    assert defi.headers["WWW-Authenticate"] == "NTLM " + base64.b64encode(b"defi").decode()

    # Identifiée, mais pas administratrice : l'état de l'index lui est refusé
    reponse = client.get("/stats", headers=ntlm(b"reponse"))
    assert reponse.status_code == 403
    assert COOKIE in reponse.cookies

    # La session suffit ensuite, sans renégocier
    assert client.get("/stats").status_code == 403


def test_jeton_refuse(client):
    reponse = client.get("/", headers=ntlm(b"faux"))
    assert reponse.status_code == 401
    assert reponse.headers["WWW-Authenticate"] == "NTLM"


@pytest.mark.parametrize("entete", ["Basic dXNlcjpwYXNz", "NTLM", "NTLM pas-du-base64!", "Negotiate bmVnb2NpYXRpb24="])
def test_entete_inattendu(client, entete):
    assert client.get("/", headers={"Authorization": entete}).status_code == 401


def test_cookie_inconnu(client):
    client.cookies.set(COOKIE, "invente")
    assert client.get("/").status_code == 401


@pytest.mark.skipif(sys.platform != "win32", reason="SSPI : Windows uniquement")
def test_vraie_negociation_ntlm_avec_la_session_courante():
    """De bout en bout avec le compte Windows qui lance les tests : client SSPI -> serveur SSPI."""
    import sspi

    app = creer_app(Config(url_base="postgresql://ged@127.0.0.1:1/ged", sources=[]))
    http = TestClient(app)
    client_sspi = sspi.ClientAuth("NTLM")

    _, tampons = client_sspi.authorize(None)
    defi = http.get("/stats", headers=ntlm(tampons[0].Buffer))
    assert defi.status_code == 401
    schema, jeton = defi.headers["WWW-Authenticate"].split(" ")
    assert schema == "NTLM"

    _, tampons = client_sspi.authorize(base64.b64decode(jeton))
    reponse = http.get("/stats", headers=ntlm(tampons[0].Buffer))
    # Identifiée (cookie de session), pas administratrice de la GED (aucun groupe configuré)
    assert reponse.status_code == 403
    assert COOKIE in reponse.cookies
