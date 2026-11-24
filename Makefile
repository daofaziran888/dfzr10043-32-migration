.PHONY: install backend frontend drill test

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm ci

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

drill:
	cd backend && .venv/bin/python -m scripts.drill_version_binding

test:
	cd backend && .venv/bin/python -m pytest -q
