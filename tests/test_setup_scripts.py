"""セットアップスクリプトの回帰テスト.

Windows で実際に壊れた箇所を固定する。
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PS1 = ROOT / "setup.ps1"
SH = ROOT / "setup.sh"


def test_ps1_has_utf8_bom():
    """setup.ps1 は UTF-8 BOM 付きでなければならない.

    Windows PowerShell 5.1 は BOM の無い .ps1 を CP932 (日本語 ANSI) として
    読むため、UTF-8 の日本語が全て文字化けする。文字化けすると引用符や
    here-string の対応が崩れ、文字列の中身がコードとして解釈されて
    大量の構文エラーになる (実機で発生済み)。
    """
    assert PS1.read_bytes().startswith(b"\xef\xbb\xbf"), (
        "setup.ps1 に UTF-8 BOM がありません。"
        "Windows PowerShell 5.1 で日本語が文字化けし構文エラーになります"
    )


def test_ps1_uses_crlf():
    """Windows 向けなので改行は CRLF に揃える."""
    b = PS1.read_bytes()
    lone_lf = b.count(b"\n") - b.count(b"\r\n")
    assert lone_lf == 0, f"CRLF でない改行が {lone_lf} 個あります"


def test_ps1_does_not_assign_automatic_variable():
    """$args は PowerShell の自動変数なので代入してはいけない."""
    text = PS1.read_text(encoding="utf-8-sig")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue          # 説明コメントは対象外
        assert "$args =" not in stripped, f"自動変数 $args への代入: {stripped}"


def test_ps1_does_not_stop_on_native_stderr():
    """ErrorActionPreference=Stop は native コマンドの stderr で異常終了する.

    Windows PowerShell 5.1 では、pip が警告を 1 行出しただけで
    NativeCommandError となりセットアップが中断してしまう。
    """
    text = PS1.read_text(encoding="utf-8-sig")
    assert '$ErrorActionPreference = "Stop"' not in text


def test_ps1_uses_windows_venv_path():
    """Windows の仮想環境は Scripts\\ 配下 (bin/ ではない)."""
    text = PS1.read_text(encoding="utf-8-sig")
    assert r".venv\Scripts" in text
    assert ".venv/bin" not in text


def test_sh_uses_lf_and_no_bom():
    """bash は BOM を解釈できず、CRLF だと実行に失敗する."""
    b = SH.read_bytes()
    assert not b.startswith(b"\xef\xbb\xbf"), "setup.sh に BOM があります"
    assert b.count(b"\r\n") == 0, "setup.sh に CRLF があります"


def test_gitattributes_pins_script_encoding():
    """git が改行や BOM を書き換えないよう固定されていること."""
    ga = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.ps1" in ga and "crlf" in ga
    assert "*.sh" in ga and "eol=lf" in ga


@pytest.mark.parametrize("path", [PS1, SH])
def test_scripts_are_not_empty(path):
    assert path.exists() and len(path.read_bytes()) > 500
