#!/usr/bin/env bash
# ttradar セットアップ (macOS / Linux)
#
#   bash setup.sh
#
# 仮想環境の作成・依存のインストール・Chromium の取得・設定ファイル生成・
# 動作診断までを一度に行う。何度実行しても壊れない。
set -u

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; RED=$'\033[31m'
YELLOW=$'\033[33m'; RESET=$'\033[0m'

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s▸ %s%s\n' "$BOLD" "$*" "$RESET"; }
ok()   { printf '%s  ✓ %s%s\n' "$GREEN" "$*" "$RESET"; }
warn() { printf '%s  ! %s%s\n' "$YELLOW" "$*" "$RESET"; }
die()  { printf '\n%s✗ %s%s\n\n' "$RED" "$*" "$RESET"; exit 1; }

cd "$(dirname "$0")" || die "スクリプトの場所に移動できませんでした"

say ""
say "${BOLD}ttradar セットアップ${RESET}"
say "${DIM}TikTok 商品紹介トレンドレーダー${RESET}"

# ------------------------------------------------------------------ Python
step "Python を確認しています"
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  say ""
  die "Python 3.10 以上が見つかりません。

  macOS なら次のどちらかで入ります:
    1) https://www.python.org/downloads/ からインストーラをダウンロード
    2) Homebrew を使っているなら:  brew install python

  インストール後、ターミナルを開き直してもう一度 bash setup.sh を実行してください。"
fi
ok "$($PY --version) を使用します"

# ------------------------------------------------------------------ 仮想環境
step "仮想環境を作成しています (.venv)"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv || die "仮想環境を作成できませんでした"
  ok "作成しました"
else
  ok "既存のものを使用します"
fi
VPY=".venv/bin/python"
[ -x "$VPY" ] || die ".venv が壊れています。rm -rf .venv してからやり直してください"

# ------------------------------------------------------------------ 依存
step "必要なライブラリをインストールしています (数分かかります)"
"$VPY" -m pip install --quiet --upgrade pip >/dev/null 2>&1
if ! "$VPY" -m pip install --quiet -e ".[browser]"; then
  die "ライブラリのインストールに失敗しました。ネットワーク接続を確認してください"
fi
ok "インストール完了"

# ------------------------------------------------------------------ ブラウザ
step "Chromium を取得しています (初回のみ・約200MB)"
if "$VPY" -m playwright install chromium >/dev/null 2>&1; then
  ok "取得完了"
else
  warn "Chromium の取得に失敗しました"
  warn "後で手動で:  .venv/bin/python -m playwright install chromium"
fi

# ------------------------------------------------------------------ 設定
step "設定ファイルを用意しています"
if [ ! -f config.yaml ]; then
  .venv/bin/ttradar init >/dev/null 2>&1 && ok "config.yaml を作成しました"
else
  ok "config.yaml は既にあります"
fi

# ------------------------------------------------------------------ 診断
step "動作を診断しています"
say ""
.venv/bin/ttradar doctor
DOCTOR=$?

say ""
say "${BOLD}────────────────────────────────────────────${RESET}"
if [ "$DOCTOR" -eq 0 ]; then
  say "${GREEN}${BOLD}TikTok に接続できました。そのまま使えます。${RESET}"
  say ""
  say "  次のコマンドでアプリが開きます:"
  say "    ${BOLD}.venv/bin/ttradar serve --interval 120 --collect-now${RESET}"
else
  say "${YELLOW}${BOLD}TikTok に接続できませんでした。${RESET}"
  say ""
  say "  上の [TikTok への到達性] の欄を確認してください。"
  say "  会社/学校のネットワークや VPN が原因のことが多いです。"
  say ""
  say "  接続できなくても、サンプルデータで画面は確認できます:"
  say "    ${BOLD}.venv/bin/ttradar demo${RESET}"
fi
say ""
say "  ${DIM}アプリを止めるときは Ctrl+C${RESET}"
say ""
