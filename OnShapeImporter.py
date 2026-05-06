"""
OnShape Importer — Fusion 360 Add-in
=====================================
Reads fusion_queue.json written by the OnShape Part Selector tool,
downloads each part from OnShape as a STEP file, and imports it into
a new Fusion 360 document (N copies per part as specified by quantity).

Installation:
    Copy the OnShapeImporter/ folder to:
    Windows: %APPDATA%\\Autodesk\\Autodesk Fusion 360\\API\\AddIns\\
    Mac:     ~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/

    Then in Fusion 360: Tools → Add-Ins → Add-Ins tab → select OnShapeImporter → Run

Usage:
    Switch to the Manufacture workspace.
    Click "Import OnShape Queue" in the toolbar.
"""

import adsk.core
import adsk.fusion
import adsk.cam
import traceback
import json
import os
import tempfile
import time
import base64
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ADDIN_NAME   = "OnShapeImporter"
CMD_ID       = "onshapeImportQueueCmd"
ONSHAPE_BASE = "https://cad.onshape.com/api/v6"

# Queue file written by the OnShape Part Selector — same folder as the .bat launcher.
# The add-in reads the path from an environment variable (set by the launcher)
# or falls back to a path stored alongside this add-in file.
QUEUE_ENV_VAR = "FRC_FUSION_QUEUE"

_app      = None
_ui       = None
_handlers = []   # keep references alive


# ---------------------------------------------------------------------------
# OnShape API helpers (urllib — no external deps needed inside Fusion)
# ---------------------------------------------------------------------------

def _auth_headers(access_key: str, secret_key: str) -> dict:
    creds = f"{access_key}:{secret_key}"
    token = base64.b64encode(creds.encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def _http_get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _http_post(url: str, headers: dict, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _http_download(url: str, headers: dict) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _request_step_export(ak: str, sk: str, part: dict) -> str:
    """Start a STEP translation job. Returns translation ID."""
    h = _auth_headers(ak, sk)
    did = part["doc_id"]
    wid = part["workspace_id"]
    eid = part["element_id"]
    pid = part.get("part_id", "")
    unit = part.get("unit", "INCH")

    body = {
        "formatName":              "STEP",
        "storeInDocument":         False,
        "resolution":              "fine",
        "allowFaultyParts":        True,
        "sendCopyToForeignService": False,
        "unit":                    unit,
    }
    if pid:
        body["partIds"] = pid

    url = f"{ONSHAPE_BASE}/partstudios/d/{did}/w/{wid}/e/{eid}/translations"
    result = _http_post(url, h, body)
    return result["id"]


def _poll_translation(ak: str, sk: str, tid: str, timeout: int = 120) -> str:
    """Poll until done. Returns externalDataId."""
    h = _auth_headers(ak, sk)
    url = f"{ONSHAPE_BASE}/translations/{tid}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = _http_get(url, h)
        state = status.get("requestState", "")
        if state == "DONE":
            ids = status.get("resultExternalDataIds", [])
            if not ids:
                raise RuntimeError("Translation succeeded but no file was produced.")
            return ids[0]
        if state == "FAILED":
            raise RuntimeError("OnShape STEP translation failed.")
        time.sleep(2)
    raise TimeoutError("STEP export timed out after 120 seconds.")


def _download_step(ak: str, sk: str, did: str, ext_id: str) -> bytes:
    """Download the exported STEP file bytes."""
    h = _auth_headers(ak, sk)
    url = f"{ONSHAPE_BASE}/documents/d/{did}/externaldata/{ext_id}"
    return _http_download(url, h)


# ---------------------------------------------------------------------------
# Queue file helpers
# ---------------------------------------------------------------------------

def _find_queue_file() -> Path | None:
    """
    Locate fusion_queue.json. Checks:
      1. FRC_FUSION_QUEUE environment variable
      2. A path.txt file stored next to this add-in (written by the launcher)
      3. Common fallback paths
    """
    # 1. Environment variable set by launcher
    env_path = os.environ.get(QUEUE_ENV_VAR)
    if env_path and Path(env_path).exists():
        return Path(env_path)

    # 2. path.txt written by the OnShape selector next to this add-in
    addin_dir = Path(__file__).parent
    path_file = addin_dir / "queue_location.txt"
    if path_file.exists():
        stored = path_file.read_text().strip()
        if stored and Path(stored).exists():
            return Path(stored)

    # 3. Fallback — look on the Desktop
    for candidate in [
        Path.home() / "Desktop" / "fusion_queue.json",
        Path.home() / "fusion_queue.json",
    ]:
        if candidate.exists():
            return candidate

    return None


def _read_queue(path: Path) -> dict:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Core import logic
# ---------------------------------------------------------------------------

def _do_import(queue: dict, progress_cb):
    """
    For each part × quantity:
      1. Export STEP from OnShape
      2. Write to a temp file
      3. Import into the active Fusion document
      4. Delete temp file
    """
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)

    if not design:
        # No active design — create a new one
        app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
        design = adsk.fusion.Design.cast(app.activeProduct)

    import_mgr = app.importManager
    root = design.rootComponent

    ak = queue["access_key"]
    sk = queue["secret_key"]
    parts = queue["parts"]
    total_imports = sum(p.get("quantity", 1) for p in parts)
    done = 0

    errors = []

    for part in parts:
        pname = part.get("part_name", "Part")
        qty   = part.get("quantity", 1)
        progress_cb(f"Exporting from OnShape: {pname}…", done, total_imports)

        try:
            tid    = _request_step_export(ak, sk, part)
            ext_id = _poll_translation(ak, sk, tid)
            step_bytes = _download_step(ak, sk, part["doc_id"], ext_id)
        except Exception as e:
            errors.append(f"{pname}: download failed — {e}")
            done += qty
            continue

        # Write to temp file — importManager needs a file path
        with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as tmp:
            tmp.write(step_bytes)
            tmp_path = tmp.name

        try:
            for i in range(qty):
                label = f"{pname} ({i+1}/{qty})" if qty > 1 else pname
                progress_cb(f"Importing into Fusion: {label}…",
                            done, total_imports)

                step_opts = import_mgr.createSTEPImportOptions(tmp_path)
                import_mgr.importToTarget(step_opts, root)
                done += 1
                adsk.doEvents()   # keep Fusion responsive
        except Exception as e:
            errors.append(f"{pname}: Fusion import failed — {e}")
            done += qty
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    return errors


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

class OnCommandCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command
            cmd.isExecutedWhenPreEmpted = False

            on_exec = OnCommandExecuted()
            cmd.execute.add(on_exec)
            _handlers.append(on_exec)
        except Exception:
            _ui.messageBox(f"Command created error:\n{traceback.format_exc()}")


class OnCommandExecuted(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            _run_import()
        except Exception:
            _ui.messageBox(f"Import error:\n{traceback.format_exc()}")


def _run_import():
    # Find queue file
    queue_path = _find_queue_file()
    if not queue_path:
        _ui.messageBox(
            "Could not find fusion_queue.json.\n\n"
            "Make sure you clicked 'Import to Fusion 360' in the\n"
            "OnShape Part Selector before switching to Fusion.\n\n"
            "The queue file should be in the same folder as the\n"
            "'Launch OnShape Selector.bat' file.",
            "Queue File Not Found",
            adsk.core.MessageBoxButtonTypes.OKButtonType,
            adsk.core.MessageBoxIconTypes.WarningIconType,
        )
        return

    queue = _read_queue(queue_path)
    parts = queue.get("parts", [])
    if not parts:
        _ui.messageBox("The queue file is empty. Add parts in the OnShape "
                       "Part Selector and click 'Import to Fusion 360' first.")
        return

    total_qty = sum(p.get("quantity", 1) for p in parts)
    part_summary = "\n".join(
        f"  • {p.get('part_name','?')}  ×{p.get('quantity',1)}"
        f"  [{p.get('material','')}  {p.get('thickness_mm','?')} mm]"
        for p in parts
    )

    # Confirm before importing
    confirm = _ui.messageBox(
        f"Ready to import {len(parts)} part type(s) "
        f"({total_qty} total) into Fusion 360:\n\n"
        f"{part_summary}\n\n"
        "Parts will be imported into the active document.\n"
        "Continue?",
        "Import OnShape Queue",
        adsk.core.MessageBoxButtonTypes.YesNoButtonType,
        adsk.core.MessageBoxIconTypes.QuestionIconType,
    )
    if confirm != adsk.core.DialogResults.DialogYes:
        return

    # Progress via status bar
    progress = _ui.createProgressDialog()
    progress.isCancelable = False
    progress.show("Importing from OnShape…", "Starting…", 0, total_qty)

    def update_progress(msg, current, total):
        progress.progressValue = current
        progress.message = msg
        adsk.doEvents()

    errors = _do_import(queue, update_progress)
    progress.hide()

    if errors:
        err_text = "\n".join(errors)
        _ui.messageBox(
            f"Import completed with {len(errors)} error(s):\n\n{err_text}",
            "Import Finished with Errors",
            adsk.core.MessageBoxButtonTypes.OKButtonType,
            adsk.core.MessageBoxIconTypes.WarningIconType,
        )
    else:
        _ui.messageBox(
            f"Successfully imported {total_qty} part(s) into Fusion 360.",
            "Import Complete",
            adsk.core.MessageBoxButtonTypes.OKButtonType,
            adsk.core.MessageBoxIconTypes.InformationIconType,
        )


# ---------------------------------------------------------------------------
# Add-in lifecycle
# ---------------------------------------------------------------------------

def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui  = _app.userInterface

        # Remove stale command if reloading
        cmd_defs = _ui.commandDefinitions
        if cmd_defs.itemById(CMD_ID):
            cmd_defs.itemById(CMD_ID).deleteMe()

        # Create command definition
        cmd_def = cmd_defs.addButtonDefinition(
            CMD_ID,
            "Import OnShape Queue",
            "Download and import parts from the OnShape Part Selector queue",
        )

        on_created = OnCommandCreated()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        # Add button to Manufacture workspace → Manage panel
        mfg_ws = _ui.workspaces.itemById("CAMEnvironment")
        if mfg_ws:
            panel = mfg_ws.toolbarPanels.itemById("CAMManagePanel")
            if not panel:
                panel = mfg_ws.toolbarPanels.itemById("CAMSolidPanel")
            if panel:
                ctrl = panel.controls.addCommand(cmd_def)
                ctrl.isPromotedByDefault = True

        if context.get("IsApplicationStartup", False):
            _ui.messageBox(
                "OnShape Importer add-in loaded.\n\n"
                "Switch to the Manufacture workspace to see the\n"
                "'Import OnShape Queue' button in the toolbar.",
                "OnShape Importer",
            )

    except Exception:
        if _ui:
            _ui.messageBox(f"Add-in startup error:\n{traceback.format_exc()}")


def stop(context):
    try:
        cmd_defs = _ui.commandDefinitions
        if cmd_defs.itemById(CMD_ID):
            cmd_defs.itemById(CMD_ID).deleteMe()

        mfg_ws = _ui.workspaces.itemById("CAMEnvironment")
        if mfg_ws:
            for panel in mfg_ws.toolbarPanels:
                ctrl = panel.controls.itemById(CMD_ID)
                if ctrl:
                    ctrl.deleteMe()
    except Exception:
        pass
