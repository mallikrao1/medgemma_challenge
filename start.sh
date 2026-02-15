#!/bin/bash

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo "🚀 Starting AI Infrastructure Platform..."
echo "📍 Running from: $SCRIPT_DIR"

# Start backend with venv Python
cd backend
"$SCRIPT_DIR/venv/bin/python" main.py &
BACKEND_PID=$!
echo "✓ Backend started (PID: $BACKEND_PID)"

# Start frontend
cd ../frontend
npm run dev &
FRONTEND_PID=$!
echo "✓ Frontend started (PID: $FRONTEND_PID)"

cd ..

echo ""
echo "Platform running:"
echo "  Backend:  http://localhost:8000"
echo "  Frontend: http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop..."

trap "kill $BACKEND_PID $FRONTEND_PID; exit" INT TERM
wait
