"""セットアップスクリプトの回帰テスト.

Windows で実際に壊れた箇所を固定する。
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PS1 = ROOT / "setup.ps1"
SH = ROOT / "setup.sh"
# ハードコードせず自動列挙する。新しい .bat を足したときに
# 検査から漏れると、文字化けや cd 忘れがそのまま利用者に届いてしまう。
BATS = sorted(ROOT.glob("*.bat"))
COMMAND = ROOT / "start.command"


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


# ------------------------------------------------------- ダブルクリック用の起動ファイル

@pytest.mark.parametrize("bat", BATS, ids=lambda p: p.name)
def test_bat_is_cp932(bat):
    """.bat は CP932 で保存されていること.

    日本語 Windows の cmd.exe は .bat を CP932 (ANSI) として読む。
    UTF-8 で保存すると日本語が文字化けし、setup.ps1 で起きたのと
    同じ問題が cmd 側でも発生する。
    """
    raw = bat.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{bat.name} に UTF-8 BOM があります"
    try:
        text = raw.decode("cp932")
    except UnicodeDecodeError as e:
        pytest.fail(f"{bat.name} は CP932 として読めません: {e}")
    assert "ttradar" in text


@pytest.mark.parametrize("bat", BATS, ids=lambda p: p.name)
def test_bat_uses_crlf(bat):
    raw = bat.read_bytes()
    assert raw.count(b"\n") - raw.count(b"\r\n") == 0, f"{bat.name} に単独 LF があります"


@pytest.mark.parametrize("bat", BATS, ids=lambda p: p.name)
def test_bat_moves_to_own_directory(bat):
    """どこから起動されても自分の場所へ移動すること.

    ダブルクリック時のカレントディレクトリは起動元に依存するため、
    cd /d "%~dp0" が無いとファイルを見つけられない。
    """
    text = bat.read_bytes().decode("cp932")
    assert 'cd /d "%~dp0"' in text, f"{bat.name} に cd /d \"%~dp0\" がありません"


def test_command_file_is_utf8_lf_and_cds():
    """Mac の .command は UTF-8 / LF で、自分の場所へ移動すること."""
    raw = COMMAND.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\r\n") == 0
    text = raw.decode("utf-8")
    assert text.startswith("#!")
    assert 'cd "$(dirname "$0")"' in text


def test_launchers_are_executable():
    """Mac 側は実行権限が無いとダブルクリックできない."""
    import os
    for p in (COMMAND, SH):
        assert os.access(p, os.X_OK), f"{p.name} に実行権限がありません"


def test_ps1_does_not_pass_quoted_code_to_native_command():
    """ネイティブコマンドへ二重引用符を含む文字列を渡さないこと.

    Windows PowerShell 5.1 は native コマンドへの引数から
    文字列内の二重引用符を落とす。そのため

        python -c 'import sys; print("%d.%d" % sys.version_info[:2])'

    は Python 側で print(%d.%d % ...) となり SyntaxError で落ちる。
    実機では、正常にインストールされている Python 3.14 まで
    「動作せず」と誤判定された。
    バージョン取得は --version を使い、コードを渡さない。
    """
    text = PS1.read_text(encoding="utf-8-sig")
    assert "--version" in text, "バージョン取得に --version を使っていません"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "-c " not in stripped:
            continue
        # native コマンド呼び出し行に限って検査する
        if "&" not in stripped:
            continue
        assert '\\"' not in stripped and '"' not in stripped.split("-c ", 1)[1][:60], (
            f"native コマンドへ引用符付きコードを渡しています: {stripped}"
        )
