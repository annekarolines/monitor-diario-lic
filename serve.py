#!/usr/bin/env python3
"""Servidor local simples para o painel de Licitações de Comunicação."""
import http.server
import webbrowser
import os

PORT = 8766
os.chdir(os.path.dirname(os.path.abspath(__file__)))

class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silencia logs

print(f"Licitações de Comunicação rodando em http://localhost:{PORT}")
print("Pressione Ctrl+C para parar.\n")

webbrowser.open(f"http://localhost:{PORT}")
http.server.HTTPServer(("", PORT), Handler).serve_forever()
