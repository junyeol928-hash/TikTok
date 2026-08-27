#!/usr/bin/env bash
# ttradar 起動 (Finder でダブルクリック)
cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/ttradar" ]; then
  echo ""
  echo "  セットアップがまだ済んでいません。"
  echo "  ターミナルで次を実行してください:"
  echo "    cd \"$(pwd)\" && bash setup.sh"
  echo ""
  read -r -p "  Enter キーで閉じます"
  exit 1
fi

echo ""
echo "  ttradar を起動します。ブラウザが自動で開きます。"
echo "  終了するには Ctrl+C"
echo ""
.venv/bin/ttradar serve --interval 120 --collect-now
