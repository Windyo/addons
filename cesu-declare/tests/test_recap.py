"""Dashboard history and recap parsing, on text captured from the live site on 2026-11-24."""
from logic import declared_in, parse_recap, recap_problem, recent_declarations

DASHBOARD = (
    "Mes références\nGEOFFREY X\nN° Cesu : Z1\nMes dernières déclarations\n"
    "Octobre 2026\tALEXANDRA GANTIER\nSeptembre 2026\tALEXANDRA GANTIER\nAoût 2026\tALEXANDRA GANTIER\n"
    "Mes salariés\nALEXANDRA GANTIER\nMes prélèvements\nMon dernier prélèvement :\n192,00 € le 03 novembre 2026\n")

RECAP = (
    "AccueilTableau de bordDéclarerRécapitulatif de votre déclaration Déclarer Tableau de bord Étape un sur trois "
    "1 Déclarer Étape deux sur trois 2 Enregistrer Étape trois sur trois 3 Envoyée CESU +ACTIVÉ ALEXANDRA GANTIER "
    "Novembre 2026 Entretien de la maison et travaux ménagers Heures effectuées : 11 h Salaire horaire net : 16,00 € "
    "Montant des cotisations : 133,01 € Impôt sur le revenu du salarié : 0,00 € Montant des cotisations et de "
    "l'impôt sur le revenu du salarié : 133,01 € Ce montant sera prélevé le 29/01/2027 Total net déclaré : 176,00 € "
    "Impôt sur le revenu du salarié : 0,00 € Salaire net à verser : 176,00 € Après déduction de l'impôt sur le "
    "revenu et de l'acompte Ce montant sera prélevé le 27/11/2026 et sera versé par le Centre national Cesu à "
    "votre salarié le 30/11/2026 FR56 **** Je valide ces informations et autorise le Cesu à présenter un ordre de "
    "prélèvement pour le salaire sur le compte bancaire indiqué.")


def test_history_box():
    h = recent_declarations(DASHBOARD)
    assert h == "Octobre 2026 ALEXANDRA GANTIER Septembre 2026 ALEXANDRA GANTIER Août 2026 ALEXANDRA GANTIER"
    assert declared_in(h, "Octobre 2026", "ALEXANDRA")
    assert declared_in(h, "Août 2026", "alexandra")
    assert not declared_in(h, "Novembre 2026", "ALEXANDRA")
    assert not declared_in(h, "Octobre 2026", "BOB")


def test_history_box_missing():
    assert recent_declarations("Accueil Se connecter") is None
    assert recent_declarations(None) is None


def test_history_row_does_not_leak_into_next_row():
    h = "Novembre 2026 BOB MARTIN Octobre 2026 ALEXANDRA GANTIER"
    assert not declared_in(h, "Novembre 2026", "ALEXANDRA")


def test_parse_recap_live_text():
    v = parse_recap(RECAP)
    assert v == {"hours": 11.0, "rate": 16.0, "total": 176.0, "net_to_pay": 176.0,
                 "contributions": 133.01, "paid_on": "30/11/2026"}


def test_recap_problem():
    v = parse_recap(RECAP)
    assert recap_problem(v, 11, 16) == ""
    assert recap_problem(v, 11, 16.004) == ""  # typed as 16,00
    assert "hours" in recap_problem(v, 12, 16)
    assert "rate" in recap_problem(v, 11, 16.5) and "total" in recap_problem(v, 11, 16.5)
    assert recap_problem(parse_recap("nothing"), 11, 16) != ""


def test_parse_recap_thousands():
    v = parse_recap("Total net déclaré : 1 234,50 € Heures effectuées : 75 h Salaire horaire net : 16,46 €")
    assert v["total"] == 1234.5 and v["hours"] == 75 and v["rate"] == 16.46
