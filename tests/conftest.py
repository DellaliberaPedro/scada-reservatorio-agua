"""Põe a raiz do projeto no sys.path para os testes importarem `models.*` (rode: python -m pytest tests -q)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
