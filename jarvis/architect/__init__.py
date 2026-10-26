"""JARVIS Architect: a 2D plan (image, PDF, DXF, sketch) → an editable parametric BIM model (FreeCAD on Open CASCADE)
→ its look (Blender) → JARVIS's «Архитектура» workspace, edited by voice or in the window. See ADR-010."""
from .service import Architect, ArchitectError

__all__ = ["Architect", "ArchitectError"]
