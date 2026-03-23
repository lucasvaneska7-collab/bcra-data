#!/bin/bash
# Dashboard Monetario Argentina - Launch Script
# Double-click this file or run: ./start.sh

cd "$(dirname "$0")"

# Check if Python 3 is available
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed."
    echo "Install it from https://www.python.org/downloads/"
    read -p "Press Enter to exit..."
    exit 1
fi

# Install dependencies if needed
if ! python3 -c "import flask" 2>/dev/null; then
    echo "Installing dependencies..."
    pip3 install -r requirements.txt
fi

echo ""
echo "============================================"
echo "  Starting Dashboard Monetario Argentina"
echo "  Open: http://localhost:5000"
echo "  Press Ctrl+C to stop"
echo "============================================"
echo ""

# Open browser automatically after a short delay
(sleep 2 && open "http://localhost:5000") &

python3 app.py
