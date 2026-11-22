"""Contrôles statiques des scripts livrés (aucun PowerShell nécessaire)."""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
PS1_FILES = sorted(SCRIPTS.glob("*.ps1"))
UTF8_BOM = b"\xef\xbb\xbf"


def test_powershell_scripts_exist():
    names = {p.name for p in PS1_FILES}
    assert {"install.ps1", "start.ps1"} <= names


@pytest.mark.parametrize("path", PS1_FILES, ids=lambda p: p.name)
def test_powershell_scripts_are_utf8_with_bom(path):
    data = path.read_bytes()
    assert data.startswith(UTF8_BOM), "Windows PowerShell 5.1 a besoin du BOM pour les accents"
    data[len(UTF8_BOM) :].decode("utf-8")
