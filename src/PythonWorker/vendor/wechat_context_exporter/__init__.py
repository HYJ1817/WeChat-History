"""Minimal vendored slice of wechat-ai-memory (MIT).

Only the Qt-free data models and text exporter are included. The upstream
package __init__ imports ExportService (which pulls in rendering/PDF/Qt-era
dependencies), so this file intentionally stays empty.
"""
