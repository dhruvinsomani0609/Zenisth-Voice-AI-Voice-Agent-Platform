import asyncio
import sys
import os
from pathlib import Path

# Add the 'app' directory to sys.path so that imports like 'from agent.settings' work
app_dir = str(Path(__file__).parent / "app")
if app_dir not in sys.path:
    sys.path.append(app_dir)

from server import main

if __name__ == "__main__":
    asyncio.run(main())
