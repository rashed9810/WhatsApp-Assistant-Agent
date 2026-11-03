"""
run_step5.py — start the Step 5 brain.

In VS Code: open this file and press the ▶ "Run Python File" button (top right).
It restarts by itself whenever you save a .py file in the brain folder.
Stop it with Ctrl+C in the terminal (or the trash-can icon on the terminal).

Then start the bridge as before: Run and Debug -> "Bridge: Step 3 (with approval)".
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if __name__ == "__main__":
    try:
        import langgraph  # noqa: F401
        import uvicorn
    except ImportError:
        print("Step 5 packages are missing. In the terminal, inside the brain folder, run:\n"
              "    pip install -r requirements_step5.txt")
        sys.exit(1)

    os.chdir(HERE)
    print("Starting the Step 5 brain on http://127.0.0.1:8000  (API page: http://127.0.0.1:8000/docs)")
    uvicorn.run("main_v5:app", host="127.0.0.1", port=8000, reload=True,
                app_dir=str(HERE), reload_dirs=[str(HERE)])
