"""scorelib: score computation engine for GUI-designed optimization scores.

__version__ is the engine's release marker: bump it when syncing scorelib/
into the SVN scripts repository (git is the development master; SVN carries
an engine-only snapshot for optimization runs — see score_gui_ui_design.md
配置・起動形態). The design UI shows it in the sidebar and the CLI prints it
to stderr, so a mismatch between the UI's bundled engine and the SVN engine
can be spotted.
"""
__version__ = "0.1.0"
