.PHONY: help install install-all demo doctor collect report run test clean

help:
	@echo "ttradar — TikTok トレンド監視"
	@echo ""
	@echo "  make install      コア依存をインストール"
	@echo "  make install-all  ブラウザ収集・yt-dlp も含めて全部入れる"
	@echo "  make doctor       環境と到達性を診断"
	@echo "  make demo         オフラインのサンプルで動作確認"
	@echo "  make collect      トレンドを収集"
	@echo "  make report       分析して HTML 出力"
	@echo "  make run          収集〜通知まで一括 (cron 向け)"
	@echo "  make test         テストを実行"

install:
	python3 -m venv .venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -e .

install-all: install
	.venv/bin/pip install -e ".[all,dev]"
	.venv/bin/playwright install chromium

doctor:
	.venv/bin/ttradar doctor

demo:
	.venv/bin/ttradar demo

collect:
	.venv/bin/ttradar collect

report:
	.venv/bin/ttradar report --html

run:
	.venv/bin/ttradar run

test:
	.venv/bin/python -m pytest tests/ -q

clean:
	rm -rf __pycache__ .pytest_cache *.egg-info build dist
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
