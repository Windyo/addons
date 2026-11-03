"""v0.1 safety net: the application code must not contain any way to submit a declaration."""
import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "rootfs" / "app"
FORBIDDEN = re.compile(r"valider|envoyer|confirmer|signer|soumettre|transmettre|submit|dry_run\s*(is|==)\s*False", re.I)
ALLOWED_CLICK_NAMES = {"Se connecter", "Vérifier", "Continuer", "Se déconnecter"}


def test_no_submit_vocabulary_in_app_code():
    for path in APP.glob("*.py"):
        text = path.read_text(encoding="utf-8").replace("['button', 'submit', 'reset']", "")  # form-map JS only lists input types
        assert not FORBIDDEN.search(text), f"{path.name} mentions a submit-like control"


def test_clicks_are_only_navigation():
    text = (APP / "runner.py").read_text(encoding="utf-8")
    names = set(re.findall(r'get_by_role\("(?:link|button)", name="([^"]+)"\)(?:\.first)?\.click\(', text))
    names |= set(re.findall(r'(?:declare_link|login_link|logout)\.(?:first\.)?click\(', text))
    assert {n for n in names if n[0].isupper()} <= ALLOWED_CLICK_NAMES | {"Déclarer"}
    # every .click( in the file is one of the known navigation calls
    clicks = re.findall(r"\.click\(", text)
    assert len(clicks) == 5  # login link, Vérifier, Déclarer, Continuer, logout
