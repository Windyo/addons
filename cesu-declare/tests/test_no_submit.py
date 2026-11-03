"""v0.2 safety net: the application code must not contain any way to submit a declaration."""
import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "rootfs" / "app"
FORBIDDEN = re.compile(r"valider|envoyer|confirmer|signer|soumettre|transmettre|enregistrer|submit|dry_run\s*(is|==)\s*False", re.I)
ALLOWED_CLICK_NAMES = {"Se connecter", "Vérifier", "Continuer", "Étape suivante", "Se déconnecter"}


def test_no_submit_vocabulary_in_app_code():
    for path in APP.glob("*.py"):
        text = path.read_text(encoding="utf-8").replace("['button', 'submit', 'reset']", "")  # form-map JS only lists input types
        assert not FORBIDDEN.search(text), f"{path.name} mentions a submit-like control"


def test_clicks_are_only_navigation():
    text = (APP / "runner.py").read_text(encoding="utf-8")
    names = set(re.findall(r'get_by_role\("(?:link|button)", name="([^"]+)"(?:, exact=True)?\)(?:\.first)?\.click\(', text))
    names |= set(re.findall(r'(?:declare_link|login_link|logout)\.(?:first\.)?click\(', text))
    assert {n for n in names if n[0].isupper()} <= ALLOWED_CLICK_NAMES | {"Déclarer"}
    # every .click( in the file is one of the known navigation calls
    clicks = re.findall(r"\.click\(", text)
    # login link, Vérifier, Déclarer, Continuer (modal), Étape suivante, logout. `modal_btn` is the
    # Continuer button held in a variable, so it is checked by name below.
    assert len(clicks) == 6
    assert re.findall(r'modal_btn\.first\.click\(', text) and 'modal_btn = page.get_by_role("button", name="Continuer")' in text
    assert "Enregistrer" not in text and "enregistrer" not in text.lower()
