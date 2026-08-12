#!/bin/bash
# Create a virtual environment to avoid macOS system python restrictions
if [ ! -d "venv" ]; then
    echo "[!] Setting up local Python environment..."
    python3 -m venv venv
fi

# Activate it
source venv/bin/activate

echo "[!] Starting Phase 2: Web Approval Server (Running in background on port 5001)..."
python3 -W ignore scripts/approval_server.py &
SERVER_PID=$!

sleep 2

echo "[!] Starting Phase 1: Lead Scout & Drafter..."
python3 -W ignore scripts/lead_agent.py

echo ""
echo "Type 'fg' to bring the background server to the front, or press Ctrl+C to stop everything."
wait $SERVER_PID
