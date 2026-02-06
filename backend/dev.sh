export CORS_ALLOW_ORIGIN="http://localhost:5173;http://localhost:7777"
PORT="${PORT:-7777}"
uvicorn open_webui.main:app --port $PORT --host 0.0.0.0 --forwarded-allow-ips '*' --reload
