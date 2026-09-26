# PDF to DXF WebApp

High-precision web application to convert engineering and vector PDF drawings into AutoCAD-compatible DXF (R2000) files.

## Features
- **AutoCAD R2000 Compatible**: Compliant DXF structure (Tables, Layers, Blocks, Viewport auto-zoom).
- **Multi-Stream Synchronization**: Seamless coordinate transformation matrix (CTM) continuity across split PDF content streams.
- **Layer & Color Separation**: Automatically maps PDF vector colors into separate CAD layers.
- **Circle & Arc Reconstruction**: Detects radial Bezier segments and outputs native CAD `CIRCLE` entities.
- **Vercel Ready**: Runs out-of-the-box on Vercel Serverless Functions with Python (`ezdxf`).

## Tech Stack
- Frontend: Vanilla HTML5 / Modern CSS / JavaScript
- Backend: Python 3 Serverless Function (`ezdxf`)
- Deployment: Vercel

## Local Development
Run `start.bat` or:
```bash
python start_server.py
```
Open `http://localhost:8765` in your browser.
