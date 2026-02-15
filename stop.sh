#!/bin/bash
echo "🛑 Stopping services..."
pkill -f "python main.py"
pkill -f "vite"
echo "✓ Services stopped"
