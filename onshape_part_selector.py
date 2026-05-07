#!/usr/bin/env python3
"""
OnShape Part Selector — FRC Team Tool
======================================
Browse multiple OnShape documents, explore their Part Studios and parts
in a nested tree, build a master export list, and pull STEP files.

Requirements:
    pip install requests

Usage:
    python onshape_part_selector.py

API Keys:
    Generate at: https://dev-portal.onshape.com  →  API Keys
    Or: OnShape account menu → My Account → API Keys
"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import json
import os
import re
import time
import threading
import base64
from pathlib import Path

try:
    import requests
except ImportError:
    raise SystemExit("Missing dependency: run  pip install requests")

CONFIG_FILE = os.path.join(Path.home(), ".onshape_frc_config.json")
KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys.txt")


# ---------------------------------------------------------------------------
# OnShape REST API Client
# ---------------------------------------------------------------------------

class OnShapeClient:
    BASE = "https://cad.onshape.com/api/v6"

    def __init__(self, access_key: str, secret_key: str):
        self.access_key = access_key
        self.secret_key = secret_key

    def _headers(self):
        creds = f"{self.access_key}:{self.secret_key}"
        token = base64.b64encode(creds.encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def get(self, path: str, params: dict = None):
        r = requests.get(f"{self.BASE}/{path}", headers=self._headers(),
                         params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict):
        r = requests.post(f"{self.BASE}/{path}", headers=self._headers(),
                          json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def download(self, path: str) -> bytes:
        r = requests.get(f"{self.BASE}/{path}", headers=self._headers(),
                         timeout=60)
        r.raise_for_status()
        return r.content
    
    def verify_connection(self) -> dict:
        """
        Checks that the API connection is working
        Tries /sessioninfo, then /me
        """
        try:
            return self.get("users/sessioninfo")
        except Exception:
            return self.get("users/me")

    # ---- Document helpers --------------------------------------------------

    def get_teams(self):
        """Returns teams the current user belongs to."""
        return self.get("teams")

    def get_companies(self):
        """Returns companies/classrooms the current user belongs to."""
        return self.get("companies")

    def search_documents(self, owner_id: str = None) -> list:
        """
        POST /documents/search — returns documents accessible to the user.
        Works for Education/Robotics classroom accounts where GET /documents fails.
        """
        url = "https://cad.onshape.com/api/v6/documents/search"
        body = {
            "limit": 100,
            "when": "LATEST",
            "sortColumn": "modifiedAt",
            "sortOrder": "desc",
        }
        if owner_id:
            body["ownerId"] = owner_id
        r = requests.post(url, headers=self._headers(), json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("items", data.get("documents", []))

    def get_team_documents(self, team_id: str):
        """
        Returns all documents accessible to the user via the search endpoint,
        which reliably returns company/classroom documents. The user can then
        filter the list in the dialog UI.
        """
        results = []
        seen_ids = set()

        def add_items(items):
            for doc in (items or []):
                did = doc.get("id", "")
                if did and did not in seen_ids:
                    seen_ids.add(did)
                    results.append(doc)

        # Search with company owner (most reliable for Education/Robotics accounts)
        try:
            companies_raw = self.get_companies()
            company_list = companies_raw if isinstance(companies_raw, list) \
                           else companies_raw.get("items",
                                companies_raw.get("companies", []))
            for company in company_list:
                cid = company.get("id", "")
                if not cid:
                    continue
                try:
                    add_items(self.search_documents(owner_id=cid))
                except Exception:
                    pass
        except Exception:
            pass

        # Fall back to unscoped search if nothing found yet
        if not results:
            try:
                add_items(self.search_documents())
            except Exception:
                pass

        return sorted(results, key=lambda d: d.get("name", "").lower())

    def get_document(self, did: str):
        return self.get(f"documents/{did}")

    def get_workspaces(self, did: str):
        return self.get(f"documents/{did}/workspaces")

    def get_elements(self, did: str, wid: str):
        """Returns all tabs in a document (Part Studios, Assemblies, Drawings…)."""
        return self.get(f"documents/d/{did}/w/{wid}/elements")

    def get_parts(self, did: str, wid: str, eid: str):
        """Returns all solid parts inside a Part Studio."""
        return self.get(f"parts/d/{did}/w/{wid}/e/{eid}")

    def get_part_bounding_boxes(self, did: str, wid: str, eid: str) -> dict:
        """
        Get thickness for each part using the bodydetails endpoint —
        the same approach as PenguinCAM. Fetches face origins/normals
        and computes thickness as the depth range of parallel faces.
        Returns {partName: {"thickness_mm": float}}
        """
        data = self.get(
            f"partstudios/d/{did}/w/{wid}/e/{eid}/bodydetails"
        )
        self._fs_debug = str(list(data.keys()) if isinstance(data, dict) else data)[:500]

        bodies = data.get("bodies", [])
        result = {}

        for body in bodies:
            name = body.get("properties", {}).get("name", "")
            if not name:
                continue

            faces = body.get("faces", [])
            plane_faces = []

            for face in faces:
                surface = face.get("surface", {})
                if surface.get("type") != "PLANE":
                    continue
                origin = surface.get("origin", {})
                normal = surface.get("normal", {})
                area   = face.get("area", 0)
                if origin and normal:
                    plane_faces.append({
                        "area":   area,
                        "origin": origin,
                        "normal": normal,
                    })

            if len(plane_faces) < 2:
                continue

            # Use the largest planar face as the reference (top face of a flat plate)
            ref = max(plane_faces, key=lambda f: f["area"])
            nx = ref["normal"].get("x", 0)
            ny = ref["normal"].get("y", 0)
            nz = ref["normal"].get("z", 1)
            mag = (nx**2 + ny**2 + nz**2) ** 0.5
            if mag < 1e-9:
                continue
            nx, ny, nz = nx/mag, ny/mag, nz/mag

            ox = ref["origin"].get("x", 0)
            oy = ref["origin"].get("y", 0)
            oz = ref["origin"].get("z", 0)

            # Project each parallel face origin onto the reference normal.
            # Dot product = signed distance along thickness axis.
            depths = []
            for pf in plane_faces:
                fx = pf["origin"].get("x", 0) - ox
                fy = pf["origin"].get("y", 0) - oy
                fz = pf["origin"].get("z", 0) - oz
                dot = fx*nx + fy*ny + fz*nz
                depths.append(dot)

            if depths:
                # OnShape face origins are in meters — convert to mm
                thickness_mm = abs(max(depths) - min(depths)) * 1000
                result[name] = {"thickness_mm": thickness_mm}

        return result


    def _fs_unwrap(self, val):
        """
        Recursively unwrap FeatureScript API value containers.
        Handles BTFSValueArray, BTFSValueMap, BTFSValueNumber, BTFSValueString, etc.
        """
        if not isinstance(val, dict):
            return val

        bt = val.get("BTType", val.get("type", ""))
        inner = val.get("value", val)

        if "Array" in bt:
            return [self._fs_unwrap(v) for v in (inner if isinstance(inner, list) else [])]

        if "Map" in bt:
            # Map value can be a list of {key, value} pairs or a dict
            result = {}
            if isinstance(inner, list):
                for entry in inner:
                    k = entry.get("key", "")
                    v = self._fs_unwrap(entry.get("value", {}))
                    if k:
                        result[k] = v
            elif isinstance(inner, dict):
                for k, v in inner.items():
                    result[k] = self._fs_unwrap(v)
            return result

        if "Number" in bt or "Quantity" in bt:
            return float(inner) if inner is not None else 0.0

        if "String" in bt:
            return str(inner) if inner is not None else ""

        if "Boolean" in bt:
            return bool(inner)

        # Fallback — just unwrap the value field if present
        if "value" in val:
            return self._fs_unwrap(inner)

        return val

    # ---- STEP export -------------------------------------------------------

    def request_step_export(self, did: str, wid: str, eid: str,
                             part_ids: list = None,
                             unit: str = "INCH") -> str:
        """
        Starts an async STEP translation for a Part Studio (or specific parts
        within it).  Returns the translation job ID.
        """
        body = {
            "formatName": "STEP",
            "storeInDocument": False,
            "resolution": "fine",
            "allowFaultyParts": True,
            "sendCopyToForeignService": False,
            "unit": unit,
        }
        if part_ids:
            body["partIds"] = ",".join(part_ids)

        result = self.post(
            f"partstudios/d/{did}/w/{wid}/e/{eid}/translations", body)
        return result["id"]

    def poll_translation(self, translation_id: str,
                         timeout_sec: int = 120) -> str:
        """
        Polls until translation is DONE and returns the externalDataId
        needed to download the file.  Raises on failure or timeout.
        """
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            status = self.get(f"translations/{translation_id}")
            state = status.get("requestState", "")
            if state == "DONE":
                ids = status.get("resultExternalDataIds", [])
                if not ids:
                    raise RuntimeError("Translation done but no result file.")
                return ids[0]
            if state == "FAILED":
                raise RuntimeError(f"OnShape translation failed: {status}")
            time.sleep(5)   # 5s interval — saves ~60% of poll calls vs 2s
        raise TimeoutError("STEP export timed out after 120 seconds.")

    def download_step(self, did: str, external_data_id: str) -> bytes:
        return self.download(
            f"documents/d/{did}/externaldata/{external_data_id}")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def parse_onshape_url(url: str):
    """
    Extract (document_id, workspace_id | None, element_id | None)
    from an OnShape document URL.
    """
    did = re.search(r'/documents/([a-f0-9]+)', url)
    wid = re.search(r'/w/([a-f0-9]+)', url)
    eid = re.search(r'/e/([a-f0-9]+)', url)
    return (
        did.group(1) if did else None,
        wid.group(1) if wid else None,
        eid.group(1) if eid else None,
    )


# ---------------------------------------------------------------------------
# Parts Cache — avoids repeated API calls for unchanged documents
# ---------------------------------------------------------------------------

class PartsCache:
    """
    Caches get_parts and bodydetails results to disk, keyed by
    (element_id + workspace_id + microversion). If the document
    hasn't changed since the last fetch, parts and thickness are
    served from cache with zero API calls.

    Cache file: onshape_parts_cache.json (next to the script).
    """

    def __init__(self, cache_path: str):
        self._path = cache_path
        self._data: dict = {}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self._path):
                with open(self._path) as f:
                    self._data = json.load(f)
        except Exception:
            self._data = {}

    def _save(self):
        try:
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception:
            pass

    def _key(self, element_id: str, workspace_id: str, microversion: str) -> str:
        return f"{element_id}:{workspace_id}:{microversion}"

    def get_parts(self, element_id: str, workspace_id: str,
                  microversion: str) -> list | None:
        """Return cached parts list, or None if not cached / stale."""
        entry = self._data.get(self._key(element_id, workspace_id, microversion))
        return entry.get("parts") if entry else None

    def get_thickness(self, element_id: str, workspace_id: str,
                      microversion: str) -> dict | None:
        """Return cached thickness dict, or None if not cached / stale."""
        entry = self._data.get(self._key(element_id, workspace_id, microversion))
        return entry.get("thickness") if entry else None

    def save(self, element_id: str, workspace_id: str, microversion: str,
             parts: list = None, thickness: dict = None):
        """Save parts and/or thickness data for this element version."""
        key = self._key(element_id, workspace_id, microversion)
        entry = self._data.get(key, {})
        if parts is not None:
            entry["parts"] = parts
        if thickness is not None:
            entry["thickness"] = thickness
        self._data[key] = entry

        # Prune stale entries — only keep entries whose microversion
        # matches what we've seen recently (keep last 200 entries)
        if len(self._data) > 200:
            keys = list(self._data.keys())
            for old_key in keys[:-200]:
                self._data.pop(old_key, None)

        self._save()

    def invalidate_element(self, element_id: str):
        """Remove all cache entries for an element (all versions)."""
        stale = [k for k in self._data if k.startswith(f"{element_id}:")]
        for k in stale:
            self._data.pop(k)
        self._save()


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    ICON_DOC   = "📄"
    ICON_PS    = "⚙"
    ICON_PART  = "◆"
    ICON_ASM   = "🔩"
    ICON_DRAW  = "📐"
    ICON_GROUP = "📁"

    def __init__(self):
        super().__init__()
        self.title("OnShape Part Selector — FRC Team Tool")
        self.geometry("1280x760")
        self.minsize(1000, 640)
        self.configure(bg="#f0f0f0")

        self.client: OnShapeClient | None = None
        # {friendly_name: {doc_id, workspace_id, url}}
        self.saved_docs: dict = {}
        # [{ doc_id, workspace_id, element_id, element_name,
        #    part_id | None, part_name, display_label }]
        self.export_queue: list = []
        # item_id → metadata dict (for tree nodes)
        self._node_meta: dict = {}

        # Parts cache — serves parts/thickness from disk if microversion unchanged
        _cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "onshape_parts_cache.json")
        self._cache = PartsCache(_cache_path)
        # element_id → microversion string (populated when elements load)
        self._element_microversions: dict = {}
        # element_ids whose parts have already been loaded this session
        self._parts_loaded: set = set()

        self._load_config()
        self._build_ui()
        self._apply_styles()

        if not self.client:
            self.after(200, self._show_api_key_dialog)
        else:
            ak, sk = self._load_keys_from_file()
            if ak and sk:
                self._set_status("Ready — API keys loaded from keys.txt")
            else:
                self._set_status("Ready — API keys loaded from config.")

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------

    def _load_config(self):
        # Load API keys from keys.txt if it exists
        ak, sk = self._load_keys_from_file()

        # Fall back to saved config for keys if keys.txt wasn't found
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE) as f:
                    cfg = json.load(f)
                if not ak or not sk:
                    ak = cfg.get("access_key", "")
                    sk = cfg.get("secret_key", "")
                self.saved_docs = cfg.get("documents", {})
            except Exception:
                pass

        if ak and sk:
            self.client = OnShapeClient(ak, sk)

    def _load_keys_from_file(self):
        """
        Load API keys from keys.txt located next to this script.
        File format — two lines, nothing else:
            your_access_key
            your_secret_key
        Returns (access_key, secret_key) or (None, None) if not found.
        """
        if not os.path.exists(KEYS_FILE):
            return None, None
        try:
            with open(KEYS_FILE) as f:
                lines = [l.strip() for l in f.read().strip().splitlines()
                         if l.strip() and not l.strip().startswith("#")]
            if len(lines) >= 2:
                return lines[0], lines[1]
        except Exception:
            pass
        return None, None

    def _save_config(self):
        # Only save keys to config if keys.txt is not present
        # (keys.txt is the preferred source of truth)
        ak, sk = self._load_keys_from_file()
        cfg = {
            "access_key": "" if (ak and sk) else (self.client.access_key if self.client else ""),
            "secret_key": "" if (ak and sk) else (self.client.secret_key if self.client else ""),
            "documents": self.saved_docs,
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self._build_menu()

        # ── Status bar (bottom) ──────────────────────────────────────
        self._status_var = tk.StringVar(value="Starting…")
        status = tk.Label(self, textvariable=self._status_var,
                          anchor=tk.W, relief=tk.SUNKEN,
                          bg="#ddd", font=("Arial", 9), padx=6)
        status.pack(side=tk.BOTTOM, fill=tk.X)

        # ── Action bar (above status) ────────────────────────────────
        action_bar = tk.Frame(self, bg="#e8e8e8", pady=6, padx=8,
                              relief=tk.GROOVE, bd=1)
        action_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # Left — export count
        self._export_count_var = tk.StringVar(value="Export list: 0 parts")
        tk.Label(action_bar, textvariable=self._export_count_var,
                 bg="#e8e8e8", font=("Arial", 10)).pack(side=tk.LEFT)

        # Right — use a grid frame so labels sit directly beside their dropdowns
        ctrl = tk.Frame(action_bar, bg="#e8e8e8")
        ctrl.pack(side=tk.RIGHT)

        tk.Label(ctrl, text="Thickness:", bg="#e8e8e8",
                 font=("Arial", 10)).grid(row=0, column=0, padx=(0, 3), sticky=tk.W)
        self._thickness_unit_var = tk.StringVar(value="in")
        ttk.Combobox(ctrl, textvariable=self._thickness_unit_var,
                     values=["in", "mm"], state="readonly",
                     width=5).grid(row=0, column=1, padx=(0, 14))
        self._thickness_unit_var.trace_add("write", self._refresh_thickness_display)

        tk.Label(ctrl, text="Export Units:", bg="#e8e8e8",
                 font=("Arial", 10)).grid(row=0, column=2, padx=(0, 3), sticky=tk.W)
        self._units_var = tk.StringVar(value="INCH")
        ttk.Combobox(ctrl, textvariable=self._units_var,
                     values=["INCH", "MILLIMETER", "CENTIMETER", "METER", "FOOT"],
                     state="readonly", width=12).grid(row=0, column=3, padx=(0, 14))

        tk.Button(ctrl, text="Clear Export List",
                  command=self._clear_export_queue,
                  padx=10, pady=4, relief=tk.FLAT,
                  bg="#ccc").grid(row=0, column=4, padx=(0, 8))

        tk.Button(ctrl, text="🚀  Export STEP Files",
                  command=self._start_export,
                  bg="#1b5e20", fg="white", font=("Arial", 11, "bold"),
                  padx=16, pady=4, relief=tk.FLAT,
                  activebackground="#2e7d32").grid(row=0, column=5)

        # ── Three-panel layout ───────────────────────────────────────
        panes = tk.PanedWindow(self, orient=tk.HORIZONTAL,
                               sashwidth=6, sashrelief=tk.FLAT,
                               bg="#bbb")
        panes.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._build_left_panel(panes)
        self._build_mid_panel(panes)
        self._build_right_panel(panes)

    def _build_menu(self):
        menubar = tk.Menu(self)
        self.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Set / Change API Keys…",
                              command=self._show_api_key_dialog)
        file_menu.add_command(label="Test API Connection",
                              command=self._test_connection)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.quit)
        menubar.add_cascade(label="File", menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

    def _build_left_panel(self, parent):
        frame = tk.LabelFrame(parent, text=" OnShape Documents ",
                              font=("Arial", 10, "bold"),
                              padx=6, pady=6, bg="#f0f0f0")
        parent.add(frame, minsize=210, width=240)

        btn_row = tk.Frame(frame, bg="#f0f0f0")
        btn_row.pack(fill=tk.X, pady=(0, 4))
        tk.Button(btn_row, text="🔗  Add by URL",
                  command=self._add_document,
                  relief=tk.FLAT, bg="#1565c0", fg="white",
                  pady=4).pack(fill=tk.X, pady=(0, 3))
        tk.Button(btn_row, text="📋  Browse & Add Documents",
                  command=self._browse_team_documents,
                  relief=tk.FLAT, bg="#4a148c", fg="white",
                  pady=4).pack(fill=tk.X, pady=(0, 3))
        tk.Button(btn_row, text="✕  Remove Selected",
                  command=self._remove_document,
                  relief=tk.FLAT, bg="#888", fg="white",
                  pady=4).pack(fill=tk.X)

        list_frame = tk.Frame(frame, bg="#f0f0f0")
        list_frame.pack(fill=tk.BOTH, expand=True)

        sb = tk.Scrollbar(list_frame)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self._doc_listbox = tk.Listbox(list_frame, yscrollcommand=sb.set,
                                        selectmode=tk.EXTENDED,
                                        font=("Arial", 10),
                                        activestyle="dotbox",
                                        relief=tk.FLAT, bd=0)
        self._doc_listbox.pack(fill=tk.BOTH, expand=True)
        sb.config(command=self._doc_listbox.yview)
        self._doc_listbox.bind("<<ListboxSelect>>", self._on_doc_selected)
        self._refresh_doc_list()

    def _build_mid_panel(self, parent):
        frame = tk.LabelFrame(parent, text=" Document Contents ",
                              font=("Arial", 10, "bold"),
                              padx=6, pady=6, bg="#f0f0f0")
        parent.add(frame, minsize=340, width=560)

        # Filter bar
        filter_frame = tk.LabelFrame(frame, text=" Filters ",
                                      bg="#f0f0f0", font=("Arial", 9))
        filter_frame.pack(fill=tk.X, pady=(0, 6))

        # Name filter
        name_row = tk.Frame(filter_frame, bg="#f0f0f0")
        name_row.pack(fill=tk.X, padx=6, pady=(4, 2))
        tk.Label(name_row, text="Part Name:", bg="#f0f0f0",
                 width=10, anchor=tk.W).pack(side=tk.LEFT)
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", self._apply_filter)
        tk.Entry(name_row, textvariable=self._filter_var,
                 width=22).pack(side=tk.LEFT, padx=4)
        tk.Button(name_row, text="✕", width=2, relief=tk.FLAT,
                  command=lambda: self._filter_var.set("")).pack(side=tk.LEFT)

        # Material filter
        mat_row = tk.Frame(filter_frame, bg="#f0f0f0")
        mat_row.pack(fill=tk.X, padx=6, pady=(2, 6))
        tk.Label(mat_row, text="Material:", bg="#f0f0f0",
                 width=10, anchor=tk.W).pack(side=tk.LEFT)
        self._material_filter_var = tk.StringVar()
        self._material_filter_var.trace_add("write", self._apply_filter)
        self._material_combo = ttk.Combobox(mat_row,
                                             textvariable=self._material_filter_var,
                                             values=[""],
                                             state="readonly", width=20)
        self._material_combo.pack(side=tk.LEFT, padx=4)
        tk.Button(mat_row, text="✕", width=2, relief=tk.FLAT,
                  command=lambda: self._material_filter_var.set("")).pack(side=tk.LEFT)

        # Tree
        tree_frame = tk.Frame(frame, bg="#f0f0f0")
        tree_frame.pack(fill=tk.BOTH, expand=True)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)

        self._tree = ttk.Treeview(tree_frame,
                                   columns=("type", "material", "thickness"),
                                   show="tree headings",
                                   selectmode="extended",
                                   yscrollcommand=vsb.set,
                                   xscrollcommand=hsb.set)
        self._tree.heading("#0",          text="Name",      anchor=tk.W)
        self._tree.heading("type",        text="Type",      anchor=tk.W)
        self._tree.heading("material",    text="Material",  anchor=tk.W)
        self._tree.heading("thickness",   text="Thickness", anchor=tk.W)
        self._tree.column("#0",         width=280, stretch=True)
        self._tree.column("type",       width=90,  stretch=False)
        self._tree.column("material",   width=120, stretch=False)
        self._tree.column("thickness",  width=90,  stretch=False)
        self._tree.pack(fill=tk.BOTH, expand=True)
        vsb.config(command=self._tree.yview)
        hsb.config(command=self._tree.xview)

        # Lazy-load thickness when user expands a Part Studio node
        self._thickness_loaded = set()  # track which element IDs already loaded
        self._thickness_queue = []      # queue of (node, did, wid, eid) pending
        self._thickness_busy  = False   # True when a fetch is in progress
        self._tree.bind("<<TreeviewOpen>>", self._on_tree_expand)

        self._tree.tag_configure("partstudio", foreground="#0d47a1")
        self._tree.tag_configure("part",       foreground="#1b5e20")
        self._tree.tag_configure("assembly",   foreground="#4a148c")
        self._tree.tag_configure("error",      foreground="#c62828")
        self._tree.tag_configure("match",      foreground="#000000",
                                  background="#fff176")
        self._tree.tag_configure("hint",       foreground="#aaaaaa",
                                  font=("Arial", 9, "italic"))  # yellow highlight

        # Add button
        tk.Button(frame, text="→  Add Selected to Export List",
                  command=self._add_selected_to_queue,
                  bg="#0d47a1", fg="white",
                  font=("Arial", 10), pady=5, relief=tk.FLAT,
                  activebackground="#1565c0").pack(fill=tk.X, pady=(8, 0))

    def _build_right_panel(self, parent):
        frame = tk.LabelFrame(parent, text=" Export List ",
                              font=("Arial", 10, "bold"),
                              padx=6, pady=6, bg="#f0f0f0")
        parent.add(frame, minsize=320, width=420)

        # Treeview grid — Part/Material/Thickness read-only, Qty editable
        tree_frame = tk.Frame(frame, bg="#f0f0f0")
        tree_frame.pack(fill=tk.BOTH, expand=True)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._queue_tree = ttk.Treeview(
            tree_frame,
            columns=("material", "thickness", "qty"),
            show="tree headings",
            selectmode="extended",
            yscrollcommand=vsb.set,
        )
        self._queue_tree.heading("#0",        text="Part",      anchor=tk.W)
        self._queue_tree.heading("material",  text="Material",  anchor=tk.W)
        self._queue_tree.heading("thickness", text="Thickness", anchor=tk.W)
        self._queue_tree.heading("qty",       text="Qty ✎",     anchor=tk.CENTER)
        self._queue_tree.column("#0",        width=150, stretch=True)
        self._queue_tree.column("material",  width=110, stretch=False)
        self._queue_tree.column("thickness", width=80,  stretch=False)
        self._queue_tree.column("qty",       width=50,  stretch=False)
        self._queue_tree.pack(fill=tk.BOTH, expand=True)
        vsb.config(command=self._queue_tree.yview)

        # Tag: grayed text for read-only columns
        self._queue_tree.tag_configure("readonly", foreground="#999999")

        tk.Label(frame, text="Double-click Qty to set part count",
                 font=("Arial", 8), fg="#888", bg="#f0f0f0").pack(anchor=tk.W, pady=(2, 0))

        self._queue_tree.bind("<Double-1>", self._on_queue_double_click)

        # Buttons
        tk.Button(frame, text="Remove Selected",
                  command=self._remove_from_queue,
                  pady=4, relief=tk.FLAT, bg="#ccc").pack(fill=tk.X, pady=(8, 4))

        tk.Button(frame, text="🔧  Import to Fusion 360",
                  command=self._start_fusion_import,
                  bg="#1565c0", fg="white",
                  font=("Arial", 10, "bold"),
                  pady=6, relief=tk.FLAT,
                  activebackground="#0d47a1").pack(fill=tk.X)

    def _apply_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", font=("Arial", 10),
                        rowheight=24, background="#ffffff",
                        fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=("Arial", 10, "bold"))
        style.map("Treeview", background=[("selected", "#bbdefb")])

    # ------------------------------------------------------------------
    # Document management (left panel)
    # ------------------------------------------------------------------

    def _refresh_doc_list(self):
        self._doc_listbox.delete(0, tk.END)
        for name in self.saved_docs:
            self._doc_listbox.insert(tk.END, f"  {self.ICON_DOC}  {name}")

    def _add_document(self):
        if not self.client:
            messagebox.showwarning("No API Keys",
                "Please configure your API keys first (File → Set API Keys).")
            return

        url = simpledialog.askstring(
            "Add by URL",
            "Paste the OnShape document URL from your browser:\n\n"
            "Example:\n"
            "https://cad.onshape.com/documents/abc123/w/def456/e/ghi789\n\n"
            "Tip: Just copy the URL directly from the OnShape tab —\n"
            "the workspace and element parts are optional.",
            parent=self)
        if not url:
            return

        did, wid, _ = parse_onshape_url(url.strip())
        if not did:
            messagebox.showerror("Invalid URL",
                "Could not find a document ID in that URL.\n"
                "Make sure you copy the URL from the OnShape browser tab.")
            return

        # Fetch default workspace if URL didn't include one
        if not wid:
            try:
                workspaces = self.client.get_workspaces(did)
                main_ws = next((w for w in workspaces
                                if w.get("isReadOnly") is False), workspaces[0])
                wid = main_ws["id"]
            except Exception as e:
                messagebox.showerror("Error",
                    f"Could not reach this document.\n\n{e}")
                return

        # Try to get a default name from OnShape
        default_name = ""
        try:
            info = self.client.get_document(did)
            default_name = info.get("name", "")
        except Exception:
            pass

        name = simpledialog.askstring(
            "Document Name",
            "Enter a friendly name for this document:",
            initialvalue=default_name, parent=self)
        if not name:
            return

        if name in self.saved_docs:
            if not messagebox.askyesno("Duplicate",
                    f"'{name}' already exists. Overwrite?"):
                return

        self.saved_docs[name] = {
            "doc_id": did,
            "workspace_id": wid,
            "url": url.strip(),
        }
        self._save_config()
        self._refresh_doc_list()
        self._set_status(f"Added document: {name}")

    def _remove_document(self):
        sel = self._doc_listbox.curselection()
        if not sel:
            return
        # Build list of names to remove
        names = []
        for idx in sel:
            label = self._doc_listbox.get(idx)
            name = label.strip().lstrip(self.ICON_DOC).strip()
            if name in self.saved_docs:
                names.append(name)
        if not names:
            return
        prompt = (f"Remove '{names[0]}' from the saved list?"
                  if len(names) == 1
                  else f"Remove {len(names)} documents from the saved list?")
        if messagebox.askyesno("Remove", prompt):
            for name in names:
                self.saved_docs.pop(name, None)
            self._save_config()
            self._refresh_doc_list()
            self._tree.delete(*self._tree.get_children())
            self._node_meta.clear()

    def _on_doc_selected(self, _event=None):
        sel = self._doc_listbox.curselection()
        # Only load document tree when exactly one is selected
        if len(sel) != 1:
            return
        label = self._doc_listbox.get(sel[0])
        name = label.strip()
        for icon in (self.ICON_DOC, " "):
            name = name.lstrip(icon)
        name = name.strip()
        doc = self.saved_docs.get(name)
        if doc:
            self._load_document_tree(name, doc["doc_id"], doc["workspace_id"])

    def _browse_team_documents(self):
        if not self.client:
            messagebox.showwarning("No API Keys",
                "Please configure your API keys first.")
            return
        self._set_status("Opening document browser…")
        # Skip team selection entirely — go straight to the document browser
        self._show_team_picker([])

    def _show_team_picker(self, teams: list):
        """Browse and add OnShape documents — opens directly and auto-loads."""
        dlg = tk.Toplevel(self)
        dlg.title("Browse Documents")
        dlg.geometry("560x500")
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text="Browse & Add OnShape Documents",
                 font=("Arial", 11, "bold")).pack(pady=(14, 2))
        tk.Label(dlg,
                 text="All documents accessible to your account are listed below.\n"
                      "Use the filter to narrow by name, then select and add.",
                 font=("Arial", 9), fg="#555", justify=tk.CENTER).pack(pady=(0, 8))

        # Reload button + count
        load_row = tk.Frame(dlg)
        load_row.pack(fill=tk.X, padx=16, pady=(0, 4))
        load_btn = tk.Button(load_row, text="↺  Reload",
                             relief=tk.FLAT, bg="#1565c0", fg="white", padx=10)
        load_btn.pack(side=tk.LEFT)
        count_label = tk.Label(load_row, text="Loading…",
                               font=("Arial", 9), fg="#666")
        count_label.pack(side=tk.LEFT, padx=10)

        # Filter box
        filter_row = tk.Frame(dlg)
        filter_row.pack(fill=tk.X, padx=16, pady=(0, 4))
        tk.Label(filter_row, text="🔍 Filter:").pack(side=tk.LEFT)
        dlg_filter_var = tk.StringVar()
        tk.Entry(filter_row, textvariable=dlg_filter_var,
                 width=30).pack(side=tk.LEFT, padx=6)

        def apply_dlg_filter(*_):
            q = dlg_filter_var.get().strip().lower()
            docs = getattr(dlg, "_team_docs", [])
            doc_listbox.delete(0, tk.END)
            for doc in docs:
                name = doc.get("name", "Unnamed")
                if not q or q in name.lower():
                    doc_listbox.insert(tk.END, f"  {self.ICON_DOC}  {name}")

        dlg_filter_var.trace_add("write", apply_dlg_filter)
        tk.Button(filter_row, text="✕", width=2, relief=tk.FLAT,
                  command=lambda: dlg_filter_var.set("")).pack(side=tk.LEFT)

        tk.Label(dlg, text="Select one or more documents to add:",
                 font=("Arial", 10)).pack(anchor=tk.W, padx=16, pady=(4, 2))

        list_frame = tk.Frame(dlg)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=16)
        sb = tk.Scrollbar(list_frame)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        doc_listbox = tk.Listbox(list_frame, yscrollcommand=sb.set,
                                  selectmode=tk.EXTENDED,
                                  font=("Arial", 10), relief=tk.FLAT)
        doc_listbox.pack(fill=tk.BOTH, expand=True)
        sb.config(command=doc_listbox.yview)

        def populate_docs(docs):
            doc_listbox.delete(0, tk.END)
            dlg._team_docs = docs
            for doc in docs:
                doc_listbox.insert(tk.END,
                    f"  {self.ICON_DOC}  {doc.get('name', 'Unnamed')}")
            count_label.config(text=f"{len(docs)} document(s) found")
            load_btn.config(state=tk.NORMAL)

        def load_docs():
            load_btn.config(state=tk.DISABLED, text="↺  Loading…")
            count_label.config(text="Loading…")
            doc_listbox.delete(0, tk.END)

            def fetch():
                try:
                    docs = self.client.get_team_documents("")
                    self.after(0, lambda: populate_docs(docs))
                except Exception as e:
                    err_msg = str(e)
                    def show_err():
                        count_label.config(text="Error — see details")
                        load_btn.config(state=tk.NORMAL, text="↺  Reload")
                        messagebox.showerror("Load Failed",
                            f"Could not load documents:\n\n{err_msg}\n\n"
                            "Check File → Test API Connection.", parent=dlg)
                    self.after(0, show_err)

            threading.Thread(target=fetch, daemon=True).start()

        load_btn.config(command=load_docs)

        def add_selected():
            sel = doc_listbox.curselection()
            if not sel:
                messagebox.showinfo("No Selection",
                    "Select one or more documents first.", parent=dlg)
                return
            added = 0
            all_docs = getattr(dlg, "_team_docs", [])
            q = dlg_filter_var.get().strip().lower()
            visible_docs = [d for d in all_docs
                            if not q or q in d.get("name", "").lower()]
            for idx in sel:
                if idx >= len(visible_docs):
                    continue
                doc = visible_docs[idx]
                name = doc.get("name", f"Document {idx}")
                did  = doc.get("id", "")
                if not did or name in self.saved_docs:
                    continue
                try:
                    workspaces = self.client.get_workspaces(did)
                    main_ws = next((w for w in workspaces
                                    if not w.get("isReadOnly")), workspaces[0])
                    wid = main_ws["id"]
                    self.saved_docs[name] = {
                        "doc_id": did,
                        "workspace_id": wid,
                        "url": f"https://cad.onshape.com/documents/{did}",
                    }
                    added += 1
                except Exception:
                    pass

            if added:
                self._save_config()
                self._refresh_doc_list()
                self._set_status(f"Added {added} document(s).")
            dlg.destroy()

        btn_row = tk.Frame(dlg)
        btn_row.pack(pady=10)
        tk.Button(btn_row, text="Add Selected to Document List",
                  command=add_selected, relief=tk.FLAT,
                  bg="#1b5e20", fg="white", padx=12, pady=5).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_row, text="Cancel", command=dlg.destroy,
                  relief=tk.FLAT, bg="#ccc", padx=12, pady=5).pack(side=tk.LEFT)

        # Auto-load immediately on open
        self.after(100, load_docs)



    # ------------------------------------------------------------------
    # Tree loading (middle panel)
    # ------------------------------------------------------------------

    def _load_document_tree(self, doc_name: str, did: str, wid: str):
        self._tree.delete(*self._tree.get_children())
        self._node_meta.clear()
        self._thickness_loaded.clear()
        self._thickness_queue.clear()
        self._thickness_busy = False
        self._parts_loaded.clear()
        self._element_microversions.clear()
        self._set_status(f"Loading {doc_name}…")

        def worker():
            try:
                elements = self.client.get_elements(did, wid)
                # Capture per-element microversion for cache keying
                for el in elements:
                    eid = el.get("id", "")
                    mv  = el.get("microversionId", el.get("dataVersion", ""))
                    if eid and mv:
                        self._element_microversions[eid] = mv
                self.after(0, lambda: self._populate_tree(elements, did, wid))
            except Exception as e:
                self.after(0, lambda: self._set_status(f"Error loading document: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _populate_tree(self, elements: list, did: str, wid: str):
        """
        Build a nested tree:

            📁 Part Studios
                ⚙ Part Studio Name
                    ◆ Part 1  [Aluminum 6061]
                    ◆ Part 2
            📁 Assemblies
                🔩 Assembly Name
            📁 Drawings
                📐 Drawing Name
        """
        by_type = {}
        for el in elements:
            t = el.get("elementType", "OTHER")
            by_type.setdefault(t, []).append(el)

        type_config = [
            ("PARTSTUDIO", f"{self.ICON_GROUP} Part Studios",  self.ICON_PS,   "Part Studio", "partstudio"),
            ("DRAWING",    f"{self.ICON_GROUP} Drawings",      self.ICON_DRAW, "Drawing",     "drawing"),
        ]

        for type_key, group_label, item_icon, type_label, tag in type_config:
            items = by_type.get(type_key, [])
            if not items:
                continue

            group_node = self._tree.insert(
                "", tk.END,
                text=group_label,
                values=("", ""),
                open=True,
            )

            for el in items:
                eid   = el["id"]
                ename = el.get("name", "Unnamed")

                el_node = self._tree.insert(
                    group_node, tk.END,
                    text=f"  {item_icon}  {ename}",
                    values=(type_label, ""),
                    tags=(tag,),
                    open=False,
                )

                self._node_meta[el_node] = {
                    "type": tag,
                    "doc_id": did,
                    "workspace_id": wid,
                    "element_id": eid,
                    "element_name": ename,
                }

                # Opt 1: LAZY loading — don't call get_parts for every studio
                # immediately. Just insert a placeholder so the tree shows the
                # expand arrow. Parts load only when the user expands the node.
                if type_key == "PARTSTUDIO":
                    self._tree.insert(el_node, tk.END,
                                      text="  (expand to load parts)",
                                      values=("", "", ""),
                                      tags=("hint",))

        count = len(elements)
        self._set_status(f"Loaded {count} element{'s' if count != 1 else ''}.")

    def _fetch_parts(self, parent_node, did: str, wid: str,
                     eid: str, element_name: str):
        """
        Load parts for a Part Studio — checks disk cache first.
        If cache hit (microversion unchanged), zero API calls needed.
        After inserting parts, queues thickness loading.
        """
        # Remove the expand-to-load placeholder
        for child in self._tree.get_children(parent_node):
            try:
                if "hint" in self._tree.item(child, "tags"):
                    self._tree.delete(child)
            except Exception:
                pass

        placeholder = self._tree.insert(parent_node, tk.END,
                                         text="  ⏳ Loading parts…",
                                         values=("", "", ""))

        def worker():
            mv = self._element_microversions.get(eid, "")

            # Opt 3: Check cache
            cached_parts = self._cache.get_parts(eid, wid, mv) if mv else None

            if cached_parts is not None:
                cached_thick = self._cache.get_thickness(eid, wid, mv) or {}
                self.after(0, lambda: self._insert_parts(
                    parent_node, placeholder, cached_parts, did, wid, eid,
                    element_name, cached_thick, from_cache=True))
                return

            # Cache miss — fetch from OnShape
            try:
                parts = self.client.get_parts(did, wid, eid)
                if mv:
                    self._cache.save(eid, wid, mv, parts=parts)
                self.after(0, lambda: self._insert_parts(
                    parent_node, placeholder, parts, did, wid, eid,
                    element_name, {}, from_cache=False))
            except Exception as e:
                self.after(0, lambda: self._tree.item(
                    placeholder, text=f"  ⚠ Error: {e}",
                    tags=("error",)))

        threading.Thread(target=worker, daemon=True).start()

    def _insert_parts(self, parent_node, placeholder, parts: list,
                      did: str, wid: str, eid: str, element_name: str,
                      bboxes: dict, from_cache: bool = False):
        # Guard against race condition where tree was cleared before thread finished
        try:
            self._tree.delete(placeholder)
        except Exception:
            return  # Tree was reset — discard this stale result

        if not parts:
            self._tree.insert(parent_node, tk.END,
                              text="  (no solid parts)", values=("", "", ""))
            return

        for part in parts:
            pid      = part.get("partId", "")
            pname    = part.get("name", "Unnamed Part")
            material = (part.get("material") or {}).get("displayName", "")
            hidden   = part.get("isHidden", False)
            if hidden:
                continue

            # Apply thickness from cache if available
            thickness_mm  = None
            thickness_str = "—"
            cached = bboxes.get(pname, {})
            if cached and cached.get("thickness_mm"):
                thickness_mm  = cached["thickness_mm"]
                thickness_str = self._fmt_thickness(thickness_mm)

            node = self._tree.insert(
                parent_node, tk.END,
                text=f"    {self.ICON_PART}  {pname}",
                values=("Part", material, thickness_str),
                tags=("part",),
            )

            self._node_meta[node] = {
                "type": "part",
                "doc_id": did,
                "workspace_id": wid,
                "element_id": eid,
                "element_name": element_name,
                "part_id": pid,
                "part_name": pname,
                "material": material,
                "thickness_mm": thickness_mm,
            }

        # Refresh material dropdown now that parts are loaded
        self._refresh_material_dropdown()

        # Queue thickness loading — skipped if already loaded from cache
        if not from_cache or not bboxes:
            self._queue_thickness_for_node(parent_node)

    def _update_thickness(self, node, thickness_str: str, thickness_val):
        """Update the thickness column for a tree node."""
        try:
            current = self._tree.item(node, "values")
            self._tree.item(node, values=(
                current[0],          # type
                current[1],          # material
                thickness_str,       # thickness
            ))
            if node in self._node_meta:
                self._node_meta[node]["thickness_mm"] = thickness_val
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Filter (middle panel)
    # ------------------------------------------------------------------

    def _apply_filter(self, *_):
        name_query = self._filter_var.get().strip().lower()
        mat_query  = self._material_filter_var.get().strip().lower()

        # Collect all unique materials from parts for the dropdown
        materials = sorted(set(
            m for m in (
                self._node_meta[n].get("material", "")
                for n in self._node_meta
                if self._node_meta[n].get("type") == "part"
            ) if m
        ))
        current_vals = list(self._material_combo["values"])
        new_vals = [""] + materials
        if new_vals != current_vals:
            self._material_combo["values"] = new_vals

        # Reset tags
        for node, meta in self._node_meta.items():
            self._tree.item(node, tags=(meta.get("type", ""),))

        # When material filter is active, collapse all part studios first
        # so only matching ones are visible/expanded
        if mat_query:
            for node, meta in self._node_meta.items():
                if meta.get("type") == "partstudio":
                    self._tree.item(node, open=False)

        if not name_query and not mat_query:
            return

        first_match = None
        for node, meta in self._node_meta.items():
            part_name    = meta.get("part_name", "").lower()
            element_name = meta.get("element_name", "").lower()
            material     = meta.get("material", "").lower()

            name_match = (not name_query or
                          name_query in part_name or
                          name_query in element_name)
            mat_match  = (not mat_query or mat_query in material)

            if name_match and mat_match:
                self._tree.item(node, tags=("match",))
                parent = self._tree.parent(node)
                while parent:
                    self._tree.item(parent, open=True)
                    # Queue thickness load for any studio we're expanding
                    self._queue_thickness_for_node(parent)
                    parent = self._tree.parent(parent)
                if first_match is None:
                    first_match = node

        if first_match:
            self._tree.see(first_match)
            self._tree.selection_set(first_match)

    def _on_tree_expand(self, event):
        """When a Part Studio node is expanded, load parts (lazy) then thickness."""
        node = self._tree.focus()
        meta = self._node_meta.get(node)
        node_type = meta.get("type", "unknown") if meta else "not-in-meta"
        self._set_status(f"Expanded: {node_type} — "
                         f"{meta.get('element_name','?') if meta else 'n/a'}")

        if not meta or meta.get("type") != "partstudio":
            return

        eid = meta.get("element_id", "")

        # Load parts if not yet loaded for this studio
        if eid not in self._parts_loaded:
            self._parts_loaded.add(eid)
            self._fetch_parts(node,
                              meta["doc_id"], meta["workspace_id"],
                              eid, meta["element_name"])
        else:
            # Parts already loaded — go straight to thickness
            self._queue_thickness_for_node(node)

    def _queue_thickness_for_node(self, node):
        """Add a studio node to the thickness queue if not already loaded."""
        meta = self._node_meta.get(node)
        if not meta or meta.get("type") != "partstudio":
            return
        eid = meta.get("element_id", "")
        if not eid or eid in self._thickness_loaded:
            self._set_status(f"Thickness already loaded for {meta.get('element_name','?')}")
            return
        self._thickness_loaded.add(eid)
        self._thickness_queue.append(
            (node, meta["doc_id"], meta["workspace_id"], eid,
             meta.get("element_name", "")))
        self._set_status(f"Queued thickness for {meta.get('element_name','?')} ({len(self._thickness_queue)} in queue)")
        self._process_thickness_queue()

    def _process_thickness_queue(self):
        """Start the next thickness fetch if one isn't already running."""
        if self._thickness_busy or not self._thickness_queue:
            return
        self._thickness_busy = True
        node, did, wid, eid, name = self._thickness_queue.pop(0)
        self._set_status(f"Loading thickness for {name}…")
        threading.Thread(
            target=self._fetch_thickness_for_studio,
            args=(node, did, wid, eid),
            daemon=True
        ).start()

    def _fetch_thickness_for_studio(self, studio_node, did, wid, eid):
        """Fetch bounding boxes — checks cache first, saves result after."""
        try:
            mv = self._element_microversions.get(eid, "")

            # Opt 3: check thickness cache
            bboxes = self._cache.get_thickness(eid, wid, mv) if mv else None

            if bboxes is None:
                # Cache miss — call OnShape bodydetails
                bboxes = self.client.get_part_bounding_boxes(did, wid, eid)
                if mv and bboxes:
                    self._cache.save(eid, wid, mv, thickness=bboxes)

            count = len(bboxes) if bboxes else 0
            self.after(0, lambda: self._set_status(
                f"Got {count} thickness entries {'(cached)' if mv and self._cache.get_thickness(eid, wid, mv) is not None else '(from OnShape)'}, applying…"))
            self.after(0, lambda: self._apply_thickness(studio_node, bboxes or {}))
        except Exception as e:
            err = str(e)[:80]
            self.after(0, lambda: self._set_status(f"Thickness error: {err}"))
        finally:
            def next_in_queue():
                self._thickness_busy = False
                self._process_thickness_queue()
            self.after(500, next_in_queue)

    def _fmt_thickness(self, thickness_mm: float) -> str:
        """Format a thickness value in the currently selected unit."""
        if self._thickness_unit_var.get() == "in":
            return f"{thickness_mm / 25.4:.4f} in"
        return f"{thickness_mm:.2f} mm"

    def _refresh_thickness_display(self, *_):
        """Re-render all thickness values in the tree when unit toggle changes."""
        for node, meta in self._node_meta.items():
            if meta.get("type") != "part":
                continue
            t = meta.get("thickness_mm")
            if t is None:
                continue
            current = self._tree.item(node, "values")
            try:
                self._tree.item(node, values=(
                    current[0], current[1], self._fmt_thickness(t)))
            except Exception:
                pass

    def _apply_thickness(self, studio_node, bboxes: dict):
        """Apply thickness data to part nodes under a studio."""
        bbox_keys = list(bboxes.keys())
        children = self._tree.get_children(studio_node)
        part_names = [self._node_meta[c].get("part_name", "?")
                      for c in children if self._node_meta.get(c)]
        self._set_status(f"Applying: bbox_keys={bbox_keys} part_names={part_names}")

        applied = 0
        for child in children:
            meta = self._node_meta.get(child)
            if not meta or meta.get("type") != "part":
                continue
            pname = meta.get("part_name", "")
            entry = bboxes.get(pname, {})
            thickness_mm = entry.get("thickness_mm") if entry else None
            if not thickness_mm:
                continue
            meta["thickness_mm"] = thickness_mm
            current = self._tree.item(child, "values")
            try:
                self._tree.item(child, values=(
                    current[0], current[1], self._fmt_thickness(thickness_mm)))
                applied += 1
            except Exception as e:
                self._set_status(f"Tree update error: {e}")
        remaining = len(self._thickness_queue)
        if remaining:
            self._set_status(f"Applied {applied}. {remaining} studio(s) remaining…")
        else:
            self._set_status(f"Thickness loaded — {applied} of {len(part_names)} parts updated.")

    def _refresh_material_dropdown(self):
        """Called after parts load to populate the material filter dropdown."""
        materials = sorted(set(
            m for m in (
                self._node_meta[n].get("material", "")
                for n in self._node_meta
                if self._node_meta[n].get("type") == "part"
            ) if m
        ))
        self._material_combo["values"] = [""] + materials

    # ------------------------------------------------------------------
    # Export queue (right panel)
    # ------------------------------------------------------------------

    def _on_queue_double_click(self, event):
        """Inline edit of the Qty column on double-click."""
        region = self._queue_tree.identify_region(event.x, event.y)
        col    = self._queue_tree.identify_column(event.x)
        item   = self._queue_tree.identify_row(event.y)
        if region != "cell" or col != "#3" or not item:
            return
        x, y, w, h = self._queue_tree.bbox(item, "qty")
        cur_val = self._queue_tree.set(item, "qty")
        var = tk.StringVar(value=cur_val)
        entry = tk.Entry(self._queue_tree, textvariable=var,
                         width=4, justify=tk.CENTER, font=("Arial", 10))
        entry.place(x=x, y=y, width=w, height=h)
        entry.select_range(0, tk.END)
        entry.focus()

        def commit(_=None):
            try:
                qty = max(1, int(var.get().strip()))
            except ValueError:
                qty = 1
            self._queue_tree.set(item, "qty", str(qty))
            idx = self._queue_tree.index(item)
            if 0 <= idx < len(self.export_queue):
                self.export_queue[idx]["quantity"] = qty
            entry.destroy()

        entry.bind("<Return>",   commit)
        entry.bind("<Tab>",      commit)
        entry.bind("<Escape>",   lambda _: entry.destroy())
        entry.bind("<FocusOut>", commit)

    def _add_selected_to_queue(self):
        selected = self._tree.selection()
        if not selected:
            messagebox.showinfo("No Selection",
                "Select one or more parts or Part Studios in the tree first.")
            return

        added = skipped = 0
        for node in selected:
            meta = self._node_meta.get(node)
            if not meta or meta["type"] not in ("part", "partstudio"):
                continue

            key = (meta["doc_id"], meta["element_id"], meta.get("part_id", ""))
            if any((e["doc_id"], e["element_id"], e.get("part_id","")) == key
                   for e in self.export_queue):
                skipped += 1
                continue

            meta["quantity"] = 1
            self.export_queue.append(meta)

            pname     = meta.get("part_name") or meta.get("element_name", "?")
            mat       = meta.get("material", "") or "—"
            thick     = meta.get("thickness_mm")
            thick_str = self._fmt_thickness(thick) if thick else "—"

            self._queue_tree.insert("", tk.END,
                text=f"  {self.ICON_PART}  {pname}",
                values=(mat, thick_str, "1"),
                tags=("readonly",))
            added += 1

        self._update_export_count()
        msg = f"Added {added}"
        if skipped:
            msg += f", skipped {skipped} duplicate(s)"
        self._set_status(msg + f".  Total: {len(self.export_queue)}")

    def _remove_from_queue(self):
        selected = list(self._queue_tree.selection())
        if not selected:
            return
        for item in reversed(selected):
            idx = self._queue_tree.index(item)
            if 0 <= idx < len(self.export_queue):
                self.export_queue.pop(idx)
            self._queue_tree.delete(item)
        self._update_export_count()

    def _clear_export_queue(self):
        self.export_queue.clear()
        for item in self._queue_tree.get_children():
            self._queue_tree.delete(item)
        self._update_export_count()
        self._set_status("Export list cleared.")

    def _update_export_count(self):
        n = len(self.export_queue)
        self._export_count_var.set(
            f"Export list: {n} part{'s' if n != 1 else ''}")

    # ------------------------------------------------------------------
    # Fusion 360 Import
    # ------------------------------------------------------------------

    def _start_fusion_import(self):
        """Validate the queue then write fusion_queue.json for the add-in."""
        if not self.export_queue:
            messagebox.showinfo("Empty List", "Add parts to the export list first.")
            return

        # Sync qty values from tree into export_queue
        for i, iid in enumerate(self._queue_tree.get_children()):
            if i < len(self.export_queue):
                try:
                    self.export_queue[i]["quantity"] = int(
                        self._queue_tree.set(iid, "qty"))
                except ValueError:
                    self.export_queue[i]["quantity"] = 1

        # Validate quantities
        for item in self.export_queue:
            if item.get("quantity", 0) < 1:
                messagebox.showerror("Invalid Quantity",
                    f"'{item.get('part_name', 'A part')}' has an invalid quantity.\n"
                    "Set all quantities to 1 or more.")
                return

        # Warn: mixed materials
        materials = {item.get("material", "")
                     for item in self.export_queue if item.get("material")}
        if len(materials) > 1:
            mat_list = "\n".join(f"  • {m}" for m in sorted(materials))
            if not messagebox.askyesno("Mixed Materials",
                    f"Export list has {len(materials)} different materials:\n\n"
                    f"{mat_list}\n\n"
                    "All parts will go into the same Fusion document.\n"
                    "Continue?"):
                return

        # Warn: mixed thicknesses
        thicknesses = {round(item["thickness_mm"], 2)
                       for item in self.export_queue if item.get("thickness_mm")}
        if len(thicknesses) > 1:
            def _fmt(t): return f"{t:.2f} mm  ({t/25.4:.4f} in)"
            t_list = "\n".join(f"  • {_fmt(t)}" for t in sorted(thicknesses))
            if not messagebox.askyesno("Mixed Thicknesses",
                    f"Export list has {len(thicknesses)} different thicknesses:\n\n"
                    f"{t_list}\n\n"
                    "These parts may need different stock material.\n"
                    "Continue?"):
                return

        # Check Fusion is running
        if not self._is_fusion_running():
            messagebox.showerror("Fusion 360 Not Open",
                "Fusion 360 does not appear to be running.\n\n"
                "Please:\n"
                "  1. Open Fusion 360\n"
                "  2. Switch to the Manufacture workspace\n"
                "  3. Click 'Import OnShape Queue' in the toolbar\n\n"
                "Then click this button again.")
            return

        queue_path = self._write_fusion_queue()
        messagebox.showinfo("Queue Ready — Switch to Fusion 360",
            f"Import queue saved ({len(self.export_queue)} part(s)).\n\n"
            "In Fusion 360:\n"
            "  Manufacture workspace → Import OnShape Queue\n\n"
            f"Queue file:\n{queue_path}")
        self._set_status("Queue written — switch to Fusion 360 to import.")

    def _is_fusion_running(self) -> bool:
        try:
            import psutil
            for proc in psutil.process_iter(["name"]):
                name = proc.info.get("name") or ""
                if "fusion" in name.lower():
                    return True
            return False
        except ImportError:
            return True   # psutil not installed — skip check

    def _write_fusion_queue(self) -> str:
        import json, datetime
        from pathlib import Path

        # Write to a shared AppData location both the selector and add-in know about
        appdata = os.environ.get("APPDATA", str(Path.home()))
        queue_dir = Path(appdata) / "FRC_CAM"
        queue_dir.mkdir(parents=True, exist_ok=True)
        queue_path = queue_dir / "fusion_queue.json"

        # Prefer keys.txt over stored config
        ak = self.client.access_key if self.client else ""
        sk = self.client.secret_key if self.client else ""
        keys_file = Path(os.path.dirname(os.path.abspath(__file__))) / "keys.txt"
        if keys_file.exists():
            lines = [l.strip() for l in keys_file.read_text().splitlines()
                     if l.strip() and not l.strip().startswith("#")]
            if len(lines) >= 2:
                ak, sk = lines[0], lines[1]

        payload = {
            "timestamp":  datetime.datetime.now().isoformat(),
            "access_key": ak,
            "secret_key": sk,
            "parts": [
                {
                    "part_name":    item.get("part_name",
                                             item.get("element_name", "Part")),
                    "element_name": item.get("element_name", ""),
                    "doc_id":       item["doc_id"],
                    "workspace_id": item["workspace_id"],
                    "element_id":   item["element_id"],
                    "part_id":      item.get("part_id", ""),
                    "material":     item.get("material", ""),
                    "thickness_mm": item.get("thickness_mm"),
                    "quantity":     item.get("quantity", 1),
                    "unit":         self._units_var.get(),
                }
                for item in self.export_queue
            ],
        }
        queue_path.write_text(json.dumps(payload, indent=2))
        return str(queue_path)

    # ------------------------------------------------------------------
    # STEP Export
    # ------------------------------------------------------------------

    def _start_export(self):
        if not self.export_queue:
            messagebox.showinfo("Empty List",
                "Add parts to the export list before exporting.")
            return

        output_dir = filedialog.askdirectory(
            title="Choose output folder for STEP files",
            mustexist=False)
        if not output_dir:
            return

        Path(output_dir).mkdir(parents=True, exist_ok=True)

        # Confirm
        n = len(self.export_queue)
        if not messagebox.askyesno("Confirm Export",
                f"Export {n} STEP file{'s' if n>1 else ''} to:\n\n{output_dir}"):
            return

        self._set_status("Exporting…  Do not close the application.")
        threading.Thread(target=self._do_export,
                         args=(output_dir, self._units_var.get()), daemon=True).start()

    def _do_export(self, output_dir: str, unit: str):
        total   = len(self.export_queue)
        success = 0
        errors  = []

        for i, item in enumerate(self.export_queue):
            name = item.get("part_name") or item.get("element_name", f"part_{i}")
            safe = re.sub(r'[^a-zA-Z0-9 _\-]', '', name).strip() or f"part_{i}"

            self.after(0, self._set_status,
                       f"Exporting {i+1}/{total}: {name}…")

            try:
                part_ids = [item["part_id"]] if item.get("part_id") else None

                trans_id = self.client.request_step_export(
                    item["doc_id"], item["workspace_id"],
                    item["element_id"], part_ids, unit)

                ext_id   = self.client.poll_translation(trans_id)
                data     = self.client.download_step(item["doc_id"], ext_id)

                out_path = os.path.join(output_dir, f"{safe}.step")
                # Avoid collisions
                counter = 1
                while os.path.exists(out_path):
                    out_path = os.path.join(output_dir, f"{safe}_{counter}.step")
                    counter += 1

                with open(out_path, "wb") as f:
                    f.write(data)

                success += 1

            except Exception as e:
                errors.append(f"{name}: {e}")

        # Report results on main thread
        self.after(0, self._export_finished, output_dir, success, errors)

    def _export_finished(self, output_dir: str, success: int, errors: list):
        self._set_status(
            f"Export complete: {success} succeeded, {len(errors)} failed.")

        if errors:
            err_text = "\n".join(errors)
            messagebox.showwarning("Export Finished with Errors",
                f"{success} file(s) exported successfully.\n\n"
                f"Errors:\n{err_text}")
        else:
            messagebox.showinfo("Export Complete",
                f"All {success} STEP file(s) exported to:\n\n{output_dir}")

    # ------------------------------------------------------------------
    # API key dialog
    # ------------------------------------------------------------------

    def _show_api_key_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title("OnShape API Keys")
        dlg.geometry("500x280")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text="OnShape API Keys",
                 font=("Arial", 13, "bold")).pack(pady=(16, 4))
        tk.Label(dlg,
                 text="Generate keys at: https://dev-portal.onshape.com → API Keys\n"
                      "or: OnShape → Account menu → My Account → API Keys",
                 font=("Arial", 9), fg="#555").pack(pady=(0, 12))

        grid = tk.Frame(dlg)
        grid.pack(padx=24, fill=tk.X)

        tk.Label(grid, text="Access Key:", anchor=tk.W,
                 width=12).grid(row=0, column=0, sticky=tk.W, pady=6)
        ak_entry = tk.Entry(grid, width=44)
        ak_entry.grid(row=0, column=1)
        if self.client:
            ak_entry.insert(0, self.client.access_key)

        tk.Label(grid, text="Secret Key:", anchor=tk.W,
                 width=12).grid(row=1, column=0, sticky=tk.W, pady=6)
        sk_entry = tk.Entry(grid, width=44, show="●")
        sk_entry.grid(row=1, column=1)
        if self.client:
            sk_entry.insert(0, self.client.secret_key)

        def save():
            ak = ak_entry.get().strip()
            sk = sk_entry.get().strip()
            if not ak or not sk:
                messagebox.showerror("Missing Keys",
                    "Both access and secret keys are required.", parent=dlg)
                return
            self.client = OnShapeClient(ak, sk)
            self._save_config()
            self._set_status("API keys saved.")
            dlg.destroy()

        btn_row = tk.Frame(dlg)
        btn_row.pack(pady=16)
        tk.Button(btn_row, text="Save Keys", command=save,
                  bg="#1565c0", fg="white", width=14,
                  font=("Arial", 10), pady=4, relief=tk.FLAT).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_row, text="Cancel", command=dlg.destroy,
                  width=10, pady=4, relief=tk.FLAT, bg="#ccc").pack(side=tk.LEFT)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _test_connection(self):
        if not self.client:
            messagebox.showwarning("No Keys", "Set your API keys first.")
            return
        self._set_status("Testing connection…")

        def worker():
            try:
                info = self.client.verify_connection()
                name = info.get("name", info.get("email", "Unknown user"))
                self.after(0, lambda: messagebox.showinfo(
                    "Connection OK",
                    f"Successfully connected to OnShape!\n\nLogged in as: {name}"))
                self.after(0, self._set_status, "Connection OK.")
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(
                    "Connection Failed",
                    f"Could not connect:\n\n{e}\n\n"
                    "Check that your API keys are correct."))
                self.after(0, self._set_status, "Connection failed.")

        threading.Thread(target=worker, daemon=True).start()

    def _show_about(self):
        messagebox.showinfo("About",
            "OnShape Part Selector\n"
            "FRC Team Tool\n\n"
            "Browse OnShape documents and export\n"
            "selected parts as STEP files.\n\n"
            "Designed for Fusion 360 CAM integration.")

    def _set_status(self, msg: str):
        self._status_var.set(f"  {msg}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = App()
    app.mainloop()
