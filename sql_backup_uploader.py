"""
SQL Server -> S3 backup utility.

Every run:
  1. Striped FULL backup of the database into N files (default 30)
  2. RESTORE VERIFYONLY ... WITH CHECKSUM
  3. Upload all files into a NEW dated folder in the bucket:  <prefix>/YYYY-MM-DD/
  4. Only after every file is uploaded and size-checked: every OLDER dated folder
     is moved to a cheap storage class (if still STANDARD) and tagged tier=cheap,
     and a lifecycle rule deletes tagged objects after `cheap_tier_retention_days`.
     The newest folder is never tagged, so it is never deleted automatically.

Configuration (highest priority first):
  1. config.json next to the program (any key present here wins, even with an empty value)
  2. AWS SSM Parameter Store, one parameter per setting under `config_path`
     (for example /sqlbackup/bucket_name)
  3. built-in defaults (DEFAULTS below)
If SSM cannot be reached, the last successfully downloaded SSM values (Logs/config_cache.json) are used.

If a run fails during upload, running it again the same day resumes the upload
(no new backup; files already in S3 with the right size are skipped).

    SqlBackupUploader.exe          -> window with a Run button
    SqlBackupUploader.exe --auto   -> no window (Task Scheduler / scripts)

Running single steps (for checking and debugging)
  The window has a checkbox per step; on the command line use --steps.
  Steps always work on the backup set of one day (today, or --date):
    1 backup   BACKUP DATABASE ... WITH COMPRESSION into N parts
    2 verify   RESTORE VERIFYONLY ... WITH CHECKSUM of the N parts
    3 upload   upload all parts + manifest.json into <prefix>/YYYY-MM-DD/
    4 archive  retire OLDER dated folders to the cheap tier + lifecycle rules
               (refused unless today's folder is complete in S3)
    5 cleanup  wait (local_keep_sets = 0), delete safe local sets and old run logs

    SqlBackupUploader.exe --auto --steps 1,2        -> back up and verify only
    SqlBackupUploader.exe --auto --steps upload     -> upload the existing parts of today's set
    SqlBackupUploader.exe --steps 4,5               -> window, with only Archive and Clean up ticked
  With all five steps ticked (the default) the program behaves exactly like a normal scheduled run.
"""
import os
import re
import sys
import json
import time
import queue
import socket
import logging
import argparse
import threading
import shutil
import hashlib
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import boto3
try:
    import pyodbc
except ImportError:      # not needed in --test mode
    pyodbc = None
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from boto3.s3.transfer import TransferConfig

DATE_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIER_TAG = {"Key": "tier", "Value": "cheap"}
RULE_EXPIRE_ID = "SqlBackup-ExpireCheapTier"
RULE_ABORT_ID = "SqlBackup-AbortIncompleteUploads"
RULE_NONCURRENT_ID = "SqlBackup-ExpireOldVersions"
RULE_DELMARKER_ID = "SqlBackup-CleanDeleteMarkers"
OUR_RULE_IDS = (RULE_EXPIRE_ID, RULE_ABORT_ID, RULE_NONCURRENT_ID, RULE_DELMARKER_ID)
UPLOAD_CLASSES = ("STANDARD", "STANDARD_IA", "ONEZONE_IA", "INTELLIGENT_TIERING", "GLACIER_IR")
CHEAP_CLASSES = ("STANDARD_IA", "ONEZONE_IA", "INTELLIGENT_TIERING", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE")
MIN_STORAGE_DAYS = {"STANDARD_IA": 30, "ONEZONE_IA": 30, "GLACIER_IR": 90, "GLACIER": 90, "DEEP_ARCHIVE": 180}
SMALL_OBJECT_BYTES = 128 * 1024      # IA bills at least 128 KB per object: tiny files are tagged, not copied

# Safety floor: no configuration source (local file or SSM) can set retention below this many days.
MIN_RETENTION_DAYS = 30
# The five steps of a run. Each can be run on its own (checkboxes in the window, --steps on the command line).
STEP_KEYS = ("backup", "verify", "upload", "archive", "cleanup")
STEP_LABELS = {"backup": "Backup", "verify": "Verify", "upload": "Upload", "archive": "Archive", "cleanup": "Clean up"}
# share of the progress bar each step uses in a full run (start %, end %)
STEP_RANGES = {"backup": (0, 35), "verify": (35, 50), "upload": (50, 95), "archive": (95, 98), "cleanup": (98, 100)}


def parse_steps(text):
    """'all', '1,2,3', 'backup,verify' or a mix -> list of step keys in run order."""
    text = (text or "").strip().lower()
    if not text or text == "all":
        return list(STEP_KEYS)
    chosen = set()
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        if part.isdigit() and 1 <= int(part) <= len(STEP_KEYS):
            chosen.add(STEP_KEYS[int(part) - 1])
        elif part in STEP_KEYS:
            chosen.add(part)
        elif part in ("clean", "clean-up", "cleanup"):
            chosen.add("cleanup")
        else:
            raise ValueError(f"Unknown step '{part}'. Use 1-5 or {', '.join(STEP_KEYS)}.")
    return [k for k in STEP_KEYS if k in chosen]


def step_text(steps):
    return ", ".join(f"{STEP_KEYS.index(k) + 1} {STEP_LABELS[k]}" for k in steps)


# These are read from the local config.json only (needed to reach SSM, or purely local UI state).
LOCAL_ONLY_KEYS = {"aws_region", "aws_profile", "config_path", "start_mode"}

DEFAULTS = {
    "sql_server": "localhost",
    "database": "",
    "sql_username": "",              # empty = Windows authentication
    "sql_password": "",
    "backup_folder": "",
    "part_count": 30,
    "bucket_name": "",
    "s3_prefix": "",                 # "" = dated folders sit at the bucket root
    "aws_region": "us-east-1",
    "aws_profile": "",               # empty = default credential chain
    "config_path": "",               # SSM path, e.g. /sqlbackup  ("" = do not use SSM)
    "sns_topic_arn": "",
    "cheap_storage_class": "STANDARD_IA",
    "cheap_tier_retention_days": 60,
    "max_bandwidth_mb_per_s": 0,     # 0 = unlimited
    "backup_timeout_hours": 6,
    "verify_timeout_hours": 6,
    "poll_seconds": 30,
    "log_level": "INFO",
    "upload_storage_class": "STANDARD",   # STANDARD_IA = upload straight to the cheap tier (no later copy)
    "manage_lifecycle": True,             # False = never touch bucket rules (use --setup-bucket once instead)
    "noncurrent_version_days": 7,         # versioned buckets: how long overwritten/deleted versions are kept
    "local_keep_sets": 1,                 # fully uploaded backup sets to keep on local disk (0 = none)
    "local_delete_delay_seconds": 180,    # local_keep_sets = 0: wait this long after the last part is in S3, then delete
    "log_keep_days": 30,                  # delete local Logs\run_log_*.log files older than this (0 = never delete)
    "expected_backup_size_gb": 0,         # used for the free-disk-space check until a first backup has been made
    "max_backup_age_days": 9,             # --check raises an alert when the newest backup is older than this
    "price_standard_gb_month": 0.023,     # for the monthly cost estimate (US East; adjust for your region)
    "price_cheap_gb_month": 0.0125,
    "start_mode": "manual",          # "manual" = wait for the Start click, "auto" = start by itself
    "auto_start_delay_seconds": 5,   # countdown before an automatic start (can be cancelled)
    "auto_close_seconds": 0,         # auto-started runs: close the window this long after success (0 = stay open)
}


def app_dir():
    override = os.environ.get("SQLBACKUP_HOME")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """Files bundled into the exe (--add-data) are unpacked to sys._MEIPASS; otherwise next to the script."""
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), name)


def set_window_icon(win, test_mode=False):
    """Window + taskbar icon. The TEST program gets the icon with the orange 'T'."""
    try:
        import ctypes   # own taskbar group, so the TEST and the real program are not merged into one button
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "SqlBackupUploader.Test" if test_mode else "SqlBackupUploader")
    except Exception:
        pass
    for name in (("SqlBackupUploader_TEST.ico",) if test_mode else ()) + ("SqlBackupUploader.ico",):
        path = resource_path(name)
        if os.path.exists(path):
            try:
                win.iconbitmap(default=path)
                return
            except Exception:
                pass


def exe_is_test_build():
    """True when the program file is named like SqlBackupUploader_TEST.exe"""
    base = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    return os.path.splitext(os.path.basename(base))[0].lower().endswith("_test")


# ====================== configuration (local file + AWS SSM) ======================
def cast_value(key, value):
    """SSM stores text only: convert it using the type of the built-in default."""
    value = value.strip()
    default = DEFAULTS.get(key)
    try:
        if isinstance(default, bool):             # check bool before int (bool is a subclass of int)
            return value.lower() in ("true", "1", "yes", "on")
        if isinstance(default, int):
            f = float(value)
            return int(f) if f.is_integer() else f
        if isinstance(default, float):
            return float(value)
    except ValueError:
        raise ValueError(f"SSM parameter '{key}' has an invalid value: {value!r}")
    return value


def fetch_remote_config(local, profile=None):
    """Reads every parameter directly under local['config_path'].
    Returns (values, source_text, warnings). Falls back to the last good copy when SSM is unreachable."""
    path = local["config_path"].rstrip("/")
    cache = os.path.join(app_dir(), "Logs", "config_cache.json")
    warnings = []
    try:
        session = boto3.Session(profile_name=(profile or local.get("aws_profile") or None),
                                region_name=local.get("aws_region") or DEFAULTS["aws_region"])
        pager = session.client("ssm").get_paginator("get_parameters_by_path")
        remote = {}
        for page in pager.paginate(Path=path, Recursive=False, WithDecryption=True):
            for p in page["Parameters"]:
                key = p["Name"].rsplit("/", 1)[-1]
                if key in LOCAL_ONLY_KEYS:
                    warnings.append(f"SSM parameter '{key}' ignored (this setting is read from config.json only)")
                    continue
                if key not in DEFAULTS:
                    warnings.append(f"SSM parameter '{key}' ignored (unknown setting - check the spelling)")
                    continue
                remote[key] = cast_value(key, p["Value"])
        if not remote:
            warnings.append(f"No usable parameters found under {path} - using local values and defaults")
            return {}, "cloud (empty)", warnings
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(remote, fh)
        return remote, "cloud", warnings
    except ValueError:
        raise                                       # a bad value in SSM must stop the run, not be hidden by the cache
    except Exception as e:  # noqa
        if os.path.exists(cache):
            with open(cache, "r", encoding="utf-8") as fh:
                warnings.append(f"Could not read SSM ({e}) - using the last saved cloud values")
                return json.load(fh), "cache", warnings
        raise RuntimeError(f"Cannot read cloud configuration at '{path}': {e}")


def load_config(path=None, profile=None):
    path = path or os.path.join(app_dir(), "config.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing configuration file: {path}")
    with open(path, "r", encoding="utf-8") as f:
        local = json.load(f)
    # A value like "<replace: ...>" or "<optional: ...>" is a placeholder from config.example.json that
    # was never filled in: treat it as not set (optional -> default, required -> clear "is required" error).
    local = {k: v for k, v in local.items()
             if not (isinstance(v, str) and v.strip().startswith("<") and v.strip().endswith(">"))}

    remote, source, warnings = {}, "local file", []
    if str(local.get("config_path", "")).strip():
        remote, source, warnings = fetch_remote_config(local, profile)

    # Every key present in config.json wins over SSM, even when its value is an empty string
    # (for example "sns_topic_arn": "" switches notifications off even if SSM has a topic).
    local_set = dict(local)
    cfg = {**DEFAULTS, **remote, **local_set}
    cfg["_origin"] = {k: ("local" if k in local_set else "cloud" if k in remote else "default")
                      for k in DEFAULTS}
    cfg["_path"] = path
    cfg["_source"] = source
    cfg["_warnings"] = warnings

    cfg["cheap_storage_class"] = str(cfg["cheap_storage_class"]).upper()
    cfg["upload_storage_class"] = str(cfg["upload_storage_class"]).upper()
    if cfg["cheap_storage_class"] not in CHEAP_CLASSES:
        raise ValueError(f"cheap_storage_class must be one of {', '.join(CHEAP_CLASSES)}")
    if cfg["upload_storage_class"] not in UPLOAD_CLASSES:
        raise ValueError(f"upload_storage_class must be one of {', '.join(UPLOAD_CLASSES)}")
    try:
        if int(cfg["cheap_tier_retention_days"]) < MIN_RETENTION_DAYS:
            raise ValueError(f"cheap_tier_retention_days must be at least {MIN_RETENTION_DAYS} "
                             f"(now {cfg['cheap_tier_retention_days']}, from {cfg['_origin']['cheap_tier_retention_days']})")
        if int(cfg["part_count"]) < 1:
            raise ValueError("part_count must be at least 1")
    except (TypeError, ValueError) as e:
        raise ValueError(str(e))
    cfg["start_mode"] = "auto" if str(cfg["start_mode"]).lower() == "auto" else "manual"
    for key in ("database", "backup_folder", "bucket_name"):
        if not str(cfg[key]).strip():
            raise ValueError(f"'{key}' is required (set it in config.json or as an SSM parameter)")
    return cfg


def setup_logging(cfg):
    log_dir = os.path.join(app_dir(), "Logs")
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("sqlbackup")
    logger.setLevel(getattr(logging, str(cfg["log_level"]).upper(), logging.INFO))
    if not logger.handlers:
        h = RotatingFileHandler(os.path.join(log_dir, "backup_upload.log"),
                                maxBytes=50 * 1024 * 1024, backupCount=10, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(h)
    return logger


def acquire_single_instance_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 47653))
    except OSError:
        return None
    return s


def pick_odbc_driver():
    if pyodbc is None:
        raise RuntimeError('pyodbc is not installed')
    drivers = pyodbc.drivers()
    for name in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server",
                 "ODBC Driver 13 for SQL Server", "SQL Server Native Client 11.0", "SQL Server"):
        if name in drivers:
            return name
    raise RuntimeError("No SQL Server ODBC driver found. Install 'ODBC Driver 18 for SQL Server' "
                       "from Microsoft.")


def sql_quote_path(p):
    return p.replace("'", "''")


class Pipeline:
    def __init__(self, cfg, logger, log_cb=None, progress_cb=None, test_mode=False, date_override=None,
                 steps=None, stage_cb=None):
        self.steps = [k for k in STEP_KEYS if k in (steps or STEP_KEYS)]
        self.full_run = len(self.steps) == len(STEP_KEYS)
        self.stage_cb = stage_cb or (lambda step, state: None)   # step key, "active"/"done"/"skipped"/"error"
        self._cur_step = None
        self.cfg = cfg
        self.logger = logger
        self.log_cb = log_cb or (lambda m: None)
        self.progress_cb = progress_cb or (lambda pct, status: None)
        self.folder = cfg["backup_folder"]
        self.db = cfg["database"]
        self.parts = int(cfg["part_count"])
        self.bucket = cfg["bucket_name"]
        self._s3 = None
        self.test_mode = test_mode
        self.run_lines = []                  # log of THIS run only (uploaded to S3 at the end)
        self.date_override = date_override   # work on this day's backup set instead of today's

    # ---------- helpers ----------
    def log(self, msg, level=logging.INFO):
        self.logger.log(level, msg)
        self.run_lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} [{logging.getLevelName(level)}] {msg}")
        self.log_cb(f"{datetime.now():%H:%M:%S}  {msg}")

    def progress(self, pct, status):
        """`pct` is on the full-run scale (STEP_RANGES). When only some steps are ticked it is rescaled,
        so the bar still goes from 0 to 100 over the steps that actually run."""
        step = self._cur_step
        if step in STEP_RANGES:
            lo, hi = STEP_RANGES[step]
            frac = (min(max(pct, lo), hi) - lo) / (hi - lo)
            weights = {k: STEP_RANGES[k][1] - STEP_RANGES[k][0] for k in self.steps}
            total = sum(weights.values()) or 1
            before = sum(w for k, w in weights.items() if STEP_KEYS.index(k) < STEP_KEYS.index(step))
            pct = 100 * (before + frac * weights.get(step, 0)) / total
        self.progress_cb(max(0, min(100, pct)), status)

    def log_config_origin(self):
        """Shows where the important settings came from (local / cloud / default)."""
        self.log(f"Configuration source: {self.cfg.get('_source', 'local file')}  (file: {self.cfg.get('_path')})")
        for w in self.cfg.get("_warnings", []):
            self.log(f"WARNING: {w}", logging.WARNING)
        origin = self.cfg.get("_origin", {})
        for k in ("bucket_name", "s3_prefix", "part_count", "upload_storage_class",
                  "cheap_storage_class", "cheap_tier_retention_days", "manage_lifecycle",
                  "max_backup_age_days", "sns_topic_arn", "local_keep_sets", "local_delete_delay_seconds"):
            self.log(f"  {k} = {self.cfg[k]!r}  ({origin.get(k, '?')})")

    @property
    def s3(self):
        if self._s3 is None:
            session = boto3.Session(profile_name=self.cfg["aws_profile"] or None,
                                    region_name=self.cfg["aws_region"] or None)
            self._s3 = session.client("s3", config=BotoConfig(
                retries={"max_attempts": 10, "mode": "standard"},
                connect_timeout=30, read_timeout=120))
        return self._s3

    def base_prefix(self):
        base = self.cfg["s3_prefix"].strip("/")
        return base + "/" if base else ""

    def key(self, folder, name=""):
        return f"{self.base_prefix()}{folder}/{name}"

    # ---------- SQL ----------
    def _connect(self):
        c = self.cfg
        cs = (f"DRIVER={{{pick_odbc_driver()}}};SERVER={c['sql_server']};DATABASE=master;"
              "TrustServerCertificate=yes;")
        if c["sql_username"]:
            pwd = os.environ.get("SQLBACKUP_SQL_PASSWORD") or c["sql_password"]   # env var keeps it out of config files
            pwd = pwd.replace("}", "}}")                                          # braces protect ; and = in the password
            cs += f"UID={c['sql_username']};PWD={{{pwd}}};"
        else:
            cs += "Trusted_Connection=yes;"
        return pyodbc.connect(cs, autocommit=True, timeout=30)

    def _exec_with_progress(self, sql, label, timeout_hours, lo, hi):
        state = {"spid": None, "err": None}

        def worker():
            try:
                conn = self._connect()
                cur = conn.cursor()
                state["spid"] = cur.execute("SELECT @@SPID").fetchone()[0]
                cur.execute(sql)
                while cur.nextset():   # lets BACKUP/RESTORE errors surface
                    pass
                conn.close()
            except Exception as e:  # noqa
                state["err"] = e

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        deadline = time.time() + timeout_hours * 3600
        mon = None
        poll = max(5, int(self.cfg["poll_seconds"]))

        def close_mon():
            nonlocal mon
            try:
                if mon:
                    mon.close()
            except Exception:
                pass
            mon = None

        while t.is_alive():
            t.join(poll)
            if not t.is_alive():
                break
            pct = None
            try:
                if mon is None and state["spid"]:
                    mon = self._connect()
                if mon:
                    row = mon.cursor().execute(
                        "SELECT percent_complete FROM sys.dm_exec_requests WHERE session_id = ?",
                        state["spid"]).fetchone()
                    pct = float(row[0]) if row else None
            except Exception:
                close_mon()
            if pct is not None:
                self.log(f"{label} running: {pct:.1f}% complete")
                self.progress(lo + (hi - lo) * pct / 100, f"{label}: {pct:.0f}%")
            else:
                self.log(f"{label} running (no progress info yet)")
            if time.time() > deadline:
                try:
                    if mon and state["spid"]:
                        mon.cursor().execute(f"KILL {int(state['spid'])}")
                except Exception:
                    pass
                close_mon()
                t.join(60)
                raise TimeoutError(f"{label} timed out after {timeout_hours} hour(s)")
        close_mon()
        if state["err"]:
            raise RuntimeError(f"{label} failed: {state['err']}")

    # ---------- step 1 + 2: backup, verify ----------
    def part_files(self, prefix):
        return [os.path.join(self.folder, f"{prefix}_Part{i}.bak") for i in range(1, self.parts + 1)]

    def disks_clause(self, files):
        return ", ".join(f"DISK = N'{sql_quote_path(f)}'" for f in files)

    def require_local_parts(self, files, step):
        """Steps 2 and 3 need the parts made by step 1 (in this run or an earlier one)."""
        bad = [os.path.basename(f) for f in files if not os.path.exists(f) or os.path.getsize(f) == 0]
        if len(bad) == len(files):
            raise RuntimeError(f"no backup parts found for this day in {self.folder} "
                               f"(expected {os.path.basename(files[0])} ...). Run step 1 (Backup) first.")
        if bad:
            raise RuntimeError(f"{len(bad)} of {len(files)} part(s) missing or empty: "
                               f"{', '.join(bad[:5])}{' ...' if len(bad) > 5 else ''}")

    def dummy_backup(self, files, hash_file):
        """TEST MODE step 1: a 30 KB random 'database' split into N parts; its SHA-256 is saved for step 2."""
        n = len(files)
        data = os.urandom(1024 * n)                      # 30 parts x 1 KB = 30 KB
        size = len(data) // n
        self.log(f"[TEST MODE] Creating dummy {len(data) // 1024} KB file and splitting into {n} parts")
        for i, f in enumerate(files):
            chunk = data[i * size:(i + 1) * size] if i < n - 1 else data[i * size:]
            with open(f, "wb") as fh:
                fh.write(chunk)
            self.progress(35 * (i + 1) / n, f"[TEST] Writing part {i + 1} of {n}")
        with open(hash_file, "w") as fh:
            fh.write(hashlib.sha256(data).hexdigest())
        self.log(f"[TEST MODE] {n} parts written; source checksum saved to {os.path.basename(hash_file)}")

    def dummy_verify(self, files, hash_file):
        """TEST MODE step 2: join the parts again and compare with the checksum saved by step 1.
        (To see a failed verification, change or replace any part file before running step 2.)"""
        if not os.path.exists(hash_file):
            raise RuntimeError(f"[TEST MODE] {os.path.basename(hash_file)} not found - the parts were not made by "
                               "step 1 in test mode. Run step 1 (Backup) first.")
        with open(hash_file) as fh:
            src_hash = fh.read().strip()
        joined = hashlib.sha256()
        for i, f in enumerate(files):
            with open(f, "rb") as fh:
                joined.update(fh.read())
            self.progress(35 + 15 * (i + 1) / len(files), f"[TEST] Checking part {i + 1} of {len(files)}")
        if joined.hexdigest() != src_hash:
            raise RuntimeError("[TEST MODE] checksum of joined parts does not match the source - "
                               "verification FAILED")
        self.log("[TEST MODE] Checksum of joined parts matches the source - verification SUCCEEDED")

    def sql_backup(self, files):
        db_quoted = self.db.replace("]", "]]")
        self.log(f"Starting FULL backup of {self.db} into {len(files)} files (compressed, with checksum)")
        self._exec_with_progress(
            f"BACKUP DATABASE [{db_quoted}] TO {self.disks_clause(files)} "
            "WITH INIT, COMPRESSION, CHECKSUM, STATS = 10;",
            "BACKUP", float(self.cfg["backup_timeout_hours"]), 0, 35)
        bad = [f for f in files if not os.path.exists(f) or os.path.getsize(f) == 0]
        if bad:
            raise RuntimeError(f"Missing or zero-size backup files: {', '.join(bad)}")
        self.log(f"All {len(files)} backup files exist and are non-zero")

    def sql_verify(self, files):
        self.log(f"Starting RESTORE VERIFYONLY (checksum) of {len(files)} files")
        self._exec_with_progress(f"RESTORE VERIFYONLY FROM {self.disks_clause(files)} WITH CHECKSUM;",
                                 "VERIFY", float(self.cfg["verify_timeout_hours"]), 35, 50)
        self.log("Verification SUCCEEDED")

    # ---------- step 3: upload into the dated folder ----------
    def s3_sizes(self, folder):
        sizes = {}
        pager = self.s3.get_paginator("list_objects_v2")
        for page in pager.paginate(Bucket=self.bucket, Prefix=self.key(folder)):
            for o in page.get("Contents", []):
                sizes[o["Key"].rsplit("/", 1)[-1]] = o["Size"]
        return sizes

    def s3_objects(self, folder):
        """name -> (size, last_modified) for every object in the dated folder."""
        objs = {}
        pager = self.s3.get_paginator("list_objects_v2")
        for page in pager.paginate(Bucket=self.bucket, Prefix=self.key(folder)):
            for o in page.get("Contents", []):
                objs[o["Key"].rsplit("/", 1)[-1]] = (o["Size"], o["LastModified"])
        return objs

    def upload_all(self, files, folder, resume, lo=50, hi=95):
        """resume=True skips parts already in S3 with the same size AND uploaded after the local file was
        written - so a part from an earlier backup of the same day is never mistaken for the current one."""
        have = self.s3_objects(folder) if resume else {}

        def already_there(f):
            obj = have.get(os.path.basename(f))
            if not obj or obj[0] != os.path.getsize(f):
                return False
            # S3 times have whole seconds only, so compare at that precision
            local_mtime = datetime.fromtimestamp(int(os.path.getmtime(f)), timezone.utc)
            return obj[1] >= local_mtime

        pending = [f for f in files if not already_there(f)]
        self.last_upload_count = len(pending)
        if len(pending) < len(files):
            self.log(f"{len(files) - len(pending)} part(s) are already in S3 from an earlier attempt - skipped")
        total = sum(os.path.getsize(f) for f in pending)
        self.log(f"Uploading {len(pending)} of {len(files)} file(s) "
                 f"({total / 1024 ** 3:.1f} GB) to s3://{self.bucket}/{self.key(folder)}")

        mbps = float(self.cfg["max_bandwidth_mb_per_s"])
        tcfg = TransferConfig(multipart_threshold=64 * 1024 ** 2, multipart_chunksize=64 * 1024 ** 2,
                              max_concurrency=10,
                              max_bandwidth=int(mbps * 1024 ** 2) if mbps > 0 else None)
        done = {"bytes": 0}
        lock = threading.Lock()

        for path in pending:
            name = os.path.basename(path)
            size = os.path.getsize(path)
            key = self.key(folder, name)
            self.log(f"Uploading {name} ({size / 1024 ** 3:.1f} GB)")

            def cb(nbytes, name=name):
                with lock:
                    done["bytes"] += nbytes
                    pct = lo + (hi - lo) * done["bytes"] / max(total, 1)
                self.progress(pct, f"Uploading {name}")

            extra = {} if self.cfg["upload_storage_class"] == "STANDARD" else {"StorageClass": self.cfg["upload_storage_class"]}
            self.s3.upload_file(path, self.bucket, key, Config=tcfg, Callback=cb, ExtraArgs=extra or None)
            remote = self.s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
            if remote != size:
                raise RuntimeError(f"Size mismatch after upload for {name}: local {size}, S3 {remote}")
            self.log(f"Uploaded and size-verified: {name}")

        # final check: every part is in S3 with the right size
        have = self.s3_sizes(folder)
        missing = [os.path.basename(f) for f in files if have.get(os.path.basename(f)) != os.path.getsize(f)]
        if missing:
            raise RuntimeError(f"Upload incomplete, missing/wrong size in S3: {', '.join(missing)}")

        manifest = {"database": self.db, "created": datetime.now().isoformat(timespec="seconds"),
                    "parts": {os.path.basename(f): os.path.getsize(f) for f in files}}
        self.s3.put_object(Bucket=self.bucket, Key=self.key(folder, "manifest.json"),
                           Body=json.dumps(manifest, indent=2).encode("utf-8"))
        self.log(f"All {len(files)} files are in S3 (manifest.json written)")

    # ---------- step 4: move older dated folders to the cheap tier ----------
    def other_date_folders(self, current_folder):
        base = self.base_prefix()
        found = []
        pager = self.s3.get_paginator("list_objects_v2")
        for page in pager.paginate(Bucket=self.bucket, Prefix=base, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                name = cp["Prefix"][len(base):].strip("/")
                if DATE_FOLDER_RE.match(name) and name != current_folder:
                    found.append(name)
        return sorted(found)

    def tier_old_folders(self, current_folder):
        """Retires every OLDER dated folder: tags it for expiry and, if still STANDARD, copies it to the
        cheap class. Folders that are newer than the current one are never touched (protects the latest
        backup if the PC clock is wrong or a test date is used). Returns (objects_copied, objects_tagged_only)."""
        target = self.cfg["cheap_storage_class"]
        all_other = self.other_date_folders(current_folder)
        newer = [f for f in all_other if f > current_folder]
        if newer:
            self.log(f"WARNING: S3 contains folder(s) NEWER than {current_folder}: {', '.join(newer)}. "
                     "They are left untouched. Check the PC date/time.", logging.WARNING)
        folders = [f for f in all_other if f < current_folder]
        if not folders:
            self.log("No older dated folders to move to the cheap tier.")
            return 0, 0
        tcfg = TransferConfig(multipart_threshold=256 * 1024 ** 2, multipart_chunksize=256 * 1024 ** 2,
                              max_concurrency=10)
        copied = tagged = 0
        for f in folders:
            to_copy, to_tag = [], []
            pager = self.s3.get_paginator("list_objects_v2")
            for page in pager.paginate(Bucket=self.bucket, Prefix=self.key(f)):
                for o in page.get("Contents", []):
                    if o["Key"].endswith("/"):
                        continue
                    cls = o.get("StorageClass", "STANDARD")
                    if cls == target or (cls == "STANDARD" and o["Size"] < SMALL_OBJECT_BYTES):
                        to_tag.append(o["Key"])      # already cheap, or too small to be worth copying - only needs the tag
                    elif cls == "STANDARD":
                        to_copy.append(o["Key"])
            self.log(f"Folder {f}: {len(to_copy)} object(s) to move to {target}, "
                     f"{len(to_tag)} to tag only (checking tag)")
            for key in to_copy:
                # in-place copy with a new storage class; tag so the expiry rule can find it
                self.s3.copy({"Bucket": self.bucket, "Key": key}, self.bucket, key,
                             ExtraArgs={"StorageClass": target, "MetadataDirective": "COPY",
                                        "Tagging": f"{TIER_TAG['Key']}={TIER_TAG['Value']}",
                                        "TaggingDirective": "REPLACE"},
                             Config=tcfg)
                copied += 1
                self.progress(97, f"Moving {f} to {target}")
            for key in to_tag:
                tags = self.s3.get_object_tagging(Bucket=self.bucket, Key=key)["TagSet"]
                if not any(t["Key"] == TIER_TAG["Key"] and t["Value"] == TIER_TAG["Value"] for t in tags):
                    self.s3.put_object_tagging(Bucket=self.bucket, Key=key,
                                               Tagging={"TagSet": tags + [TIER_TAG]})
                    tagged += 1
        self.log(f"Moved {copied} object(s) to {target}; marked {tagged} object(s) for expiry without copying")
        return copied, tagged

    def versioning_status(self):
        try:
            return self.s3.get_bucket_versioning(Bucket=self.bucket).get("Status")   # Enabled / Suspended / None
        except Exception as e:  # noqa
            self.log(f"Could not read bucket versioning status: {e}", logging.WARNING)
            return None

    def ensure_lifecycle(self, force=False):
        """Adds our rules, keeps any other rules already on the bucket."""
        if not self.cfg["manage_lifecycle"] and not force:
            self.log("manage_lifecycle is off - bucket lifecycle rules are left alone "
                     "(they are set once with --setup-bucket).")
            return
        days = int(self.cfg["cheap_tier_retention_days"])
        min_days = MIN_STORAGE_DAYS.get(self.cfg["cheap_storage_class"], 0)
        if days < min_days:
            self.log(f"WARNING: {self.cfg['cheap_storage_class']} has a {min_days}-day minimum charge; deleting "
                     f"after {days} days still bills the remaining days (early-deletion fee).", logging.WARNING)
        try:
            existing = self.s3.get_bucket_lifecycle_configuration(Bucket=self.bucket)["Rules"]
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
                raise
            existing = []
        rules = [r for r in existing if r.get("ID") not in OUR_RULE_IDS]
        base = self.base_prefix()
        flt = {"And": {"Prefix": base, "Tags": [TIER_TAG]}} if base else {"Tag": TIER_TAG}
        rules.append({"ID": RULE_EXPIRE_ID, "Status": "Enabled", "Filter": flt,
                      "Expiration": {"Days": days}})
        rules.append({"ID": RULE_ABORT_ID, "Status": "Enabled", "Filter": {"Prefix": base},
                      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}})
        versioning = self.versioning_status()
        if versioning in ("Enabled", "Suspended"):
            # With versioning, "delete" only hides the object - these rules make deletion real
            # and stop old versions (for example the pre-copy STANDARD version) from piling up and costing money.
            nc = int(self.cfg["noncurrent_version_days"])
            rules.append({"ID": RULE_NONCURRENT_ID, "Status": "Enabled", "Filter": {"Prefix": base},
                          "NoncurrentVersionExpiration": {"NoncurrentDays": nc}})
            rules.append({"ID": RULE_DELMARKER_ID, "Status": "Enabled", "Filter": {"Prefix": base},
                          "Expiration": {"ExpiredObjectDeleteMarker": True}})
        self.s3.put_bucket_lifecycle_configuration(Bucket=self.bucket,
                                                   LifecycleConfiguration={"Rules": rules})
        self.log(f"Lifecycle rules set: tagged objects expire {days} days after upload/move"
                 + (f"; old versions are removed after {self.cfg['noncurrent_version_days']} days"
                    if versioning in ("Enabled", "Suspended") else ""))

    def wait_before_local_delete(self, since):
        """Pauses so that the local files are deleted `local_delete_delay_seconds` after the upload finished."""
        remaining = int(self.cfg["local_delete_delay_seconds"]) - (time.time() - since)
        if remaining <= 0:
            return
        self.log(f"All parts are in S3. Waiting {int(remaining)} s before deleting the local backup files.")
        end = time.time() + remaining
        while True:
            left = end - time.time()
            if left <= 0:
                break
            self.progress(99, f"Deleting local files in {int(left)} s")
            time.sleep(min(5, left))

    # ---------- local log housekeeping ----------
    def cleanup_old_logs(self):
        """Deletes local per-run logs (Logs\\run_log_YYYYMMDD_HHMMSS.log) older than log_keep_days.
        Every run log is also stored in S3, so the local copies are only a convenience.
        Never touches backup_upload.log (it rotates by itself), the cache or the size file."""
        days = int(self.cfg["log_keep_days"])
        log_dir = os.path.join(app_dir(), "Logs")
        if days <= 0 or not os.path.isdir(log_dir):
            return 0
        cutoff = time.time() - days * 86400
        removed = 0
        for name in os.listdir(log_dir):
            if not re.fullmatch(r"run_log_\d{8}_\d{6}\.log", name):
                continue
            path = os.path.join(log_dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError as e:
                self.log(f"Could not delete old log {name}: {e}", logging.WARNING)
        if removed:
            self.log(f"Deleted {removed} local run log(s) older than {days} day(s)")
        return removed

    # ---------- S3 usage / cost estimate ----------
    @staticmethod
    def fmt_gb(n):
        gb = n / 1024 ** 3
        return f"{gb:.2f} GB" if gb < 1 else f"{gb:.1f} GB" if gb < 10 else f"{gb:,.0f} GB"

    def usage_line(self):
        base = self.base_prefix()
        std = cheap = 0
        folders = set()
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=base):
            for o in page.get("Contents", []):
                rest = o["Key"][len(base):]
                first = rest.split("/", 1)[0]
                if "/" in rest and DATE_FOLDER_RE.match(first):
                    folders.add(first)
                    if o.get("StorageClass", "STANDARD") == "STANDARD":
                        std += o["Size"]
                    else:
                        cheap += o["Size"]
        cost = (std / 1024 ** 3) * float(self.cfg["price_standard_gb_month"]) \
            + (cheap / 1024 ** 3) * float(self.cfg["price_cheap_gb_month"])
        return (f"S3 now holds {len(folders)} backup folder(s): {self.fmt_gb(std + cheap)} "
                f"({self.fmt_gb(std)} Standard + {self.fmt_gb(cheap)} cheap tier), about "
                f"${cost:,.2f}/month for storage (estimate).")

    # ---------- local disk housekeeping ----------
    @property
    def size_file(self):
        return os.path.join(app_dir(), "Logs", "last_backup_size.txt")

    def list_local_sets(self):
        pat = re.compile(rf"^{re.escape(self.db)}_Full_(\d{{8}})(?:_Part(\d+)\.bak|\.(verified|uploaded|sha256))$", re.I)
        sets = {}
        if not os.path.isdir(self.folder):
            return sets
        for name in os.listdir(self.folder):
            m = pat.match(name)
            if not m:
                continue
            prefix = f"{self.db}_Full_{m.group(1)}"
            info = sets.setdefault(prefix, {"date": m.group(1), "parts": {}, "markers": [],
                                            "verified": False, "uploaded": False, "sha256": False})
            path = os.path.join(self.folder, name)
            if m.group(2):
                info["parts"][int(m.group(2))] = path
            else:
                info["markers"].append(path)
                info[m.group(3).lower()] = True
        return sets

    @staticmethod
    def folder_for(info):
        d = info["date"]
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"

    def confirm_in_s3(self, info):
        """True only if every local part is in S3 with exactly the same size."""
        if len(info["parts"]) != self.parts:
            return False
        have = self.s3_sizes(self.folder_for(info))
        return all(have.get(os.path.basename(p)) == os.path.getsize(p) for p in info["parts"].values())

    def cleanup_local(self, keep, protect=None):
        """Deletes local backup files that are safe to delete. Returns bytes freed.
        - fully uploaded sets beyond the newest `keep` (each re-checked against S3 first)
        - sets that never passed verification (failed/partial backups)
        Never touches: `protect`, verified-but-not-uploaded sets, or files that don't match our naming."""
        sets = self.list_local_sets()
        freed = 0

        def remove(info, why):
            nonlocal freed
            for pth in list(info["parts"].values()) + info["markers"]:
                try:
                    size = os.path.getsize(pth)
                    os.remove(pth)
                    freed += size
                except OSError as e:
                    self.log(f"Could not delete {pth}: {e}", logging.WARNING)
            self.log(f"Deleted local set {info['date']} ({why})")

        uploaded = sorted((p for p, i in sets.items() if i["uploaded"]), reverse=True)
        kept = [sets[p]["date"] for p in uploaded[:max(0, keep)]]
        if kept:
            self.log(f"Kept {len(kept)} uploaded set(s) on local disk on purpose (local_keep_sets = {keep}): "
                     f"{', '.join(kept)}. Set local_keep_sets to 0 to delete them after upload.")
        for prefix in uploaded[max(0, keep):]:
            if prefix == protect:
                continue
            if self.confirm_in_s3(sets[prefix]):
                remove(sets[prefix], "confirmed complete in S3")
            else:
                self.log(f"Kept local set {sets[prefix]['date']}: could not confirm it is complete in S3.",
                         logging.WARNING)
        for prefix, info in sets.items():
            if prefix == protect or info["verified"]:
                continue
            remove(info, "never passed verification")
        for prefix, info in sets.items():
            if info["verified"] and not info["uploaded"] and prefix != protect:
                self.log(f"Note: local set {info['date']} is verified but not fully uploaded - kept.", logging.WARNING)
        return freed

    def finish_stuck_sets(self, protect):
        """Verified backups from earlier days whose upload never finished: finish them now."""
        sets = self.list_local_sets()
        for prefix in sorted(sets, reverse=True):
            info = sets[prefix]
            if prefix == protect or not info["verified"] or info["uploaded"]:
                continue
            if len(info["parts"]) != self.parts:
                self.log(f"Local set {info['date']} has {len(info['parts'])} parts (expected {self.parts}) - skipped.",
                         logging.WARNING)
                continue
            self.log(f"Finishing the unfinished upload of the earlier backup {info['date']}")
            try:
                files = [info["parts"][n] for n in sorted(info["parts"])]
                self.upload_all(files, self.folder_for(info), resume=True, lo=0, hi=8)
                with open(os.path.join(self.folder, prefix + ".uploaded"), "w") as fh:
                    fh.write(datetime.now().isoformat())
            except Exception as e:  # noqa
                self.log(f"Could not finish the earlier upload {info['date']}: {e}", logging.WARNING)

    def ensure_free_space(self, protect):
        os.makedirs(self.folder, exist_ok=True)
        est = int(float(self.cfg["expected_backup_size_gb"]) * 1024 ** 3)
        try:
            with open(self.size_file) as fh:
                est = max(est, int(fh.read().strip()))
        except Exception:
            pass
        if est <= 0:
            self.log("Free-space check skipped (backup size not known yet).")
            return
        need = int(est * 1.1)
        free = shutil.disk_usage(self.folder).free
        if free >= need:
            self.log(f"Free disk space OK: {self.fmt_gb(free)} free, about {self.fmt_gb(need)} needed.")
            return
        self.log(f"Low disk space: {self.fmt_gb(free)} free, about {self.fmt_gb(need)} needed - "
                 "removing old local backups that are safely in S3.", logging.WARNING)
        self.cleanup_local(keep=0, protect=protect)
        free = shutil.disk_usage(self.folder).free
        if free < need:
            raise RuntimeError(f"Not enough free disk space on the backup drive: {self.fmt_gb(free)} free, "
                               f"about {self.fmt_gb(need)} needed. Free some space and run again.")

    def record_backup_size(self, files):
        try:
            os.makedirs(os.path.dirname(self.size_file), exist_ok=True)
            with open(self.size_file, "w") as fh:
                fh.write(str(sum(os.path.getsize(f) for f in files)))
        except Exception:
            pass

    # ---------- monitoring: "has a backup happened recently?" ----------
    def check_freshness(self):
        """Run daily (ideally from another PC): alerts if the newest COMPLETE backup in S3 is too old."""
        maxage = float(self.cfg["max_backup_age_days"])
        newest = None
        for f in sorted(self.other_date_folders(""), reverse=True):
            try:
                head = self.s3.head_object(Bucket=self.bucket, Key=self.key(f, "manifest.json"))
            except ClientError as e:
                if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                    continue          # folder without manifest = incomplete upload
                raise
            newest = (f, head["LastModified"])
            break
        if not newest:
            msg = f"No complete backup found in s3://{self.bucket}/{self.base_prefix()}"
            ok = False
        else:
            age = (datetime.now(timezone.utc) - newest[1]).total_seconds() / 86400
            ok = age <= maxage
            msg = (f"Newest complete backup is {newest[0]} ({age:.1f} day(s) old; limit {maxage:g})."
                   + ("" if ok else " BACKUPS ARE OVERDUE."))
        self.log(msg, logging.INFO if ok else logging.ERROR)
        if not ok:
            self.notify(False, msg)
        return ok, msg

    # ---------- one-time bucket hardening (run by an administrator) ----------
    def setup_bucket(self, versioning=True, lock_days=0):
        results = []

        def step(name, fn):
            try:
                fn()
                results.append((True, name))
            except Exception as e:  # noqa
                results.append((False, f"{name}: {e}"))

        b = self.bucket
        step("Block all public access", lambda: self.s3.put_public_access_block(
            Bucket=b, PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True}))
        step("Default encryption (SSE-S3)", lambda: self.s3.put_bucket_encryption(
            Bucket=b, ServerSideEncryptionConfiguration={"Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}, "BucketKeyEnabled": True}]}))
        if versioning or lock_days:
            step("Versioning enabled", lambda: self.s3.put_bucket_versioning(
                Bucket=b, VersioningConfiguration={"Status": "Enabled"}))

        def tls_policy():
            try:
                pol = json.loads(self.s3.get_bucket_policy(Bucket=b)["Policy"])
            except ClientError as e:
                if e.response["Error"]["Code"] != "NoSuchBucketPolicy":
                    raise
                pol = {"Version": "2012-10-17", "Statement": []}
            if any(st.get("Sid") == "DenyInsecureTransport" for st in pol["Statement"]):
                return
            pol["Statement"].append({
                "Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
                "Resource": [f"arn:aws:s3:::{b}", f"arn:aws:s3:::{b}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}}})
            self.s3.put_bucket_policy(Bucket=b, Policy=json.dumps(pol))
        step("Only encrypted (HTTPS) connections allowed", tls_policy)

        if lock_days:
            step(f"Object Lock: nothing can be deleted for {lock_days} day(s) (governance mode)",
                 lambda: self.s3.put_object_lock_configuration(
                     Bucket=b, ObjectLockConfiguration={"ObjectLockEnabled": "Enabled", "Rule": {
                         "DefaultRetention": {"Mode": "GOVERNANCE", "Days": int(lock_days)}}}))
        step("Lifecycle rules (expiry, old versions, incomplete uploads)", lambda: self.ensure_lifecycle(force=True))
        return results

    # ---------- run log ----------
    def upload_run_log(self, folder, to_s3=True):
        """Saves this run's log locally and (to_s3) uploads it into the dated S3 folder. Never raises."""
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = f"run_log_{stamp}.log"
            local_dir = os.path.join(app_dir(), "Logs")
            os.makedirs(local_dir, exist_ok=True)
            local = os.path.join(local_dir, name)
            if to_s3:
                self.log(f"Uploading this run's log to s3://{self.bucket}/{self.key(folder, name)}")
            else:
                self.log(f"Run log saved locally only (no S3 step was selected): Logs\\{name}")
            with open(local, "w", encoding="utf-8") as fh:
                fh.write("\n".join(self.run_lines) + "\n")
            if not to_s3:
                return None
            self.s3.upload_file(local, self.bucket, self.key(folder, name),
                                ExtraArgs={"ContentType": "text/plain"})
            return name
        except Exception as e:  # noqa
            self.logger.warning(f"Could not upload run log: {e}")
            self.log_cb(f"WARNING: could not upload run log: {e}")
            return None

    # ---------- notification ----------
    def notify(self, ok, message):
        arn = self.cfg["sns_topic_arn"]
        if not arn:
            return
        try:
            session = boto3.Session(profile_name=self.cfg["aws_profile"] or None,
                                    region_name=arn.split(":")[3])
            what = "" if self.full_run else " (steps " + ",".join(
                str(STEP_KEYS.index(k) + 1) for k in self.steps) + " only)"
            test = "[TEST] " if self.test_mode else ""
            subject = f"{test}SQL Backup {'OK' if ok else 'FAILED'}{what} - {datetime.now():%Y-%m-%d}"
            session.client("sns").publish(TopicArn=arn, Subject=subject[:99],
                                          Message=f"{message}\n\nHost: {socket.gethostname()}")
            self.log("Notification sent")
        except Exception as e:  # noqa
            self.log(f"Could not send notification: {e}", logging.WARNING)

    # ---------- the five steps ----------
    # Each step works on the backup set of one day (ctx) and returns a short summary for the result message.
    @staticmethod
    def _remove(*paths):
        for pth in paths:
            if os.path.exists(pth):
                os.remove(pth)

    def step_backup(self, ctx):
        os.makedirs(self.folder, exist_ok=True)
        if self.full_run:
            self.finish_stuck_sets(protect=ctx["prefix"])   # earlier verified backups that never finished uploading
        self.ensure_free_space(protect=ctx["prefix"])       # fail early (and clean up) instead of 4 hours in
        # a new backup makes any earlier verify/upload result for this day meaningless
        self._remove(ctx["verified"], ctx["uploaded"], ctx["hash_file"])
        if self.test_mode:
            self.dummy_backup(ctx["files"], ctx["hash_file"])
        else:
            self.sql_backup(ctx["files"])
        self.record_backup_size(ctx["files"])
        ctx["backup_ran"] = True
        total = sum(os.path.getsize(f) for f in ctx["files"])
        return f"Backup created: {len(ctx['files'])} parts, {self.fmt_gb(total)}."

    def step_verify(self, ctx):
        self.require_local_parts(ctx["files"], "Verify")
        self._remove(ctx["verified"])                       # a failed verify must not leave an old 'verified' mark
        if self.test_mode:
            self.dummy_verify(ctx["files"], ctx["hash_file"])
        else:
            self.sql_verify(ctx["files"])
        with open(ctx["verified"], "w") as fh:
            fh.write(datetime.now().isoformat())
        return f"Checksum of all {len(ctx['files'])} parts verified."

    def step_upload(self, ctx):
        self.require_local_parts(ctx["files"], "Upload")
        if not os.path.exists(ctx["verified"]):
            self.log("WARNING: this backup set has NOT passed step 2 (Verify). Uploading anyway because the "
                     "step was selected on its own - do not rely on this copy until it is verified.",
                     logging.WARNING)
        if ctx["resume"]:
            self.log("Found today's verified backup that is not fully uploaded - resuming upload.")
            self.progress(50, "Resuming upload...")
        self._remove(ctx["uploaded"])
        # parts already in S3 are skipped only if they are newer than the local file (see upload_all)
        self.upload_all(ctx["files"], ctx["folder"], resume=not ctx["backup_ran"])
        with open(ctx["uploaded"], "w") as fh:
            fh.write(datetime.now().isoformat())
        ctx["t_uploaded"] = time.time()
        sent = getattr(self, "last_upload_count", len(ctx["files"]))
        return (f"All {len(ctx['files'])} files are in {self.key(ctx['folder'])} "
                f"({sent} uploaded now, {len(ctx['files']) - sent} already there).")

    def step_archive(self, ctx):
        folder = ctx["folder"]
        # Safety: older folders are only retired (and so scheduled for deletion) when the newest one is complete.
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self.key(folder, "manifest.json"))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                raise RuntimeError(f"Archive refused: s3://{self.bucket}/{self.key(folder)} has no complete upload "
                                   "(manifest.json missing). Older folders are only retired after the newest "
                                   "backup is safely in S3 - run step 3 (Upload) first.")
            raise
        self.log(f"{self.key(folder)} is complete in S3 (manifest.json found)")
        copied, tagged = self.tier_old_folders(folder)
        self.ensure_lifecycle()
        msg = (f"Older folders retired to {self.cfg['cheap_storage_class']} ({copied} copied, {tagged} tagged "
               "without copy)." if copied or tagged else "No older folders needed archiving.")
        try:
            msg += " " + self.usage_line()
        except Exception as e:  # noqa
            self.log(f"Could not calculate S3 usage: {e}", logging.WARNING)
        return msg

    def step_cleanup(self, ctx):
        keep = int(self.cfg["local_keep_sets"])
        is_uploaded = os.path.exists(ctx["uploaded"])
        if keep == 0 and is_uploaded:
            since = ctx.get("t_uploaded") or os.path.getmtime(ctx["uploaded"])
            self.wait_before_local_delete(since)
        # this day's set is only removable once it is uploaded (a backup made but not yet uploaded is kept)
        freed = self.cleanup_local(keep=keep, protect=None if is_uploaded else ctx["prefix"])
        removed_logs = self.cleanup_old_logs()
        parts = []
        if freed:
            parts.append(f"Freed {self.fmt_gb(freed)} of local disk.")
        if removed_logs:
            parts.append(f"Deleted {removed_logs} old run log(s).")
        if not parts:
            parts.append(f"Nothing deleted locally (local_keep_sets = {keep} keeps the newest uploaded set"
                         f"{'s' if keep != 1 else ''})." if keep else "Nothing to clean up locally.")
        return " ".join(parts)

    # ---------- main ----------
    def run(self):
        folder = None
        to_s3 = self.full_run or "upload" in self.steps or "archive" in self.steps
        try:
            self.log_config_origin()
            if self.test_mode:
                self.log("[TEST MODE] SQL Server is not used: step 1 makes dummy data, step 2 checks its SHA-256")
            self.log(f"Steps selected: {step_text(self.steps)}" + ("  (full run)" if self.full_run else ""))
            if self.date_override and not self.test_mode and "backup" in self.steps:
                raise RuntimeError("Step 1 Backup cannot run for another date (--date) - untick it, or run "
                                   "without --date")
            now = self.date_override or datetime.now()
            prefix = f"{self.db}_Full_{now:%Y%m%d}"
            folder = f"{now:%Y-%m-%d}"
            files = self.part_files(prefix)
            ctx = {"prefix": prefix, "folder": folder, "files": files,
                   "verified": os.path.join(self.folder, prefix + ".verified"),
                   "uploaded": os.path.join(self.folder, prefix + ".uploaded"),
                   "hash_file": os.path.join(self.folder, prefix + ".sha256"),
                   "backup_ran": False, "resume": False, "t_uploaded": None}
            self.log(f"Backup set: {prefix}  (S3 folder {self.key(folder)})")

            # full run: if today's backup was verified but its upload never finished, only resume the upload
            if self.full_run:
                ctx["resume"] = (os.path.exists(ctx["verified"]) and not os.path.exists(ctx["uploaded"])
                                 and all(os.path.exists(f) and os.path.getsize(f) > 0 for f in files))

            summary, warnings = [], []
            for key in STEP_KEYS:
                n, label = STEP_KEYS.index(key) + 1, STEP_LABELS[key]
                if key not in self.steps:
                    self.stage_cb(key, "skipped")
                    continue
                if ctx["resume"] and key in ("backup", "verify"):
                    self.log(f"Step {n} {label} skipped: today's backup is already verified (resuming the upload)")
                    self.stage_cb(key, "skipped")
                    continue
                self._cur_step = key
                self.stage_cb(key, "active")
                self.log(f"----- Step {n}: {label} -----")
                self.progress(STEP_RANGES[key][0], f"Step {n}: {label}...")
                try:
                    summary.append(getattr(self, f"step_{key}")(ctx))
                except Exception as e:  # noqa
                    self.stage_cb(key, "error")
                    # in a full (scheduled) run the new backup is already safe in S3 by now, so a problem in
                    # archive / clean up is a warning, as before. When chosen on their own they fail the run.
                    if self.full_run and key in ("archive", "cleanup"):
                        self.logger.exception(f"{label} step failed")
                        self.log(f"WARNING: step {n} {label} failed: {e}", logging.WARNING)
                        warnings.append(f"WARNING: {label} step failed: {e}")
                        continue
                    raise RuntimeError(f"Step {n} {label} failed: {e}") from e
                self.stage_cb(key, "done")
                self.log(f"Step {n} {label} finished")
            self._cur_step = None

            head = f"Backup {prefix}" + ("" if self.full_run else f" - steps {step_text(self.steps)}") + ":"
            msg = " ".join([head] + summary + warnings)
            self.log(msg)
            log_name = self.upload_run_log(folder, to_s3)
            if log_name:
                msg += f" Log: {self.key(folder, log_name)}"
            self.notify(True, msg)
            self.progress(100, "Finished")
            return True, msg
        except Exception as e:  # noqa
            self._cur_step = None
            self.logger.exception("Pipeline failed")
            msg = f"FAILED: {e}"
            self.log(msg, logging.ERROR)
            self.run_lines.append(traceback.format_exc())
            log_name = self.upload_run_log(folder, to_s3) if folder else None
            if log_name:
                msg += f" Log: {self.key(folder, log_name)}"
            self.notify(False, msg)
            return False, str(e)


# ====================== GUI ======================
def run_gui(cfg, logger, test_mode=False, date_override=None, autostart=False, selected_steps=None):
    import tkinter as tk
    from tkinter import ttk, messagebox

    try:  # crisp text on high-DPI Windows screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    NAVY, ACCENT, ACCENT_DK = "#1B2540", "#2563EB", "#1D4ED8"
    OK, BAD, WARN = "#16A34A", "#DC2626", "#D97706"
    BG, CARD, BORDER, TEXT, MUTED = "#F1F3F7", "#FFFFFF", "#DDE1E8", "#111827", "#6B7280"
    FONT = "Segoe UI"
    STEPS = [STEP_LABELS[k] for k in STEP_KEYS]

    root = tk.Tk()
    set_window_icon(root, test_mode)
    root.title("SQL Backup Uploader" + ("  -  TEST MODE" if test_mode else ""))
    root.configure(bg=BG)
    # ---- fit the window to the screen (small laptops and Windows display scaling 125 % / 150 %) ----
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    scale = max(1.0, root.winfo_fpixels("1i") / 96.0)   # 1.0 at 100 % scaling, 1.5 at 150 %
    S = lambda px: int(round(px * scale))              # design size (at 100 %) -> real pixels
    compact = sh < S(860)                              # e.g. 1366x768, or 1920x1080 at 150 %
    PAD, GAP = S(14 if compact else 24), S(8 if compact else 12)
    w = min(S(820), sw - S(40))
    h = min(S(900), sh - S(90))                        # keep the bottom buttons above the taskbar
    root.minsize(min(S(600), w), min(S(480), h))
    root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h - S(40)) // 3)}")
    wrap_labels = []                                   # labels whose text wraps to the window width

    style = ttk.Style()
    style.theme_use("clam")
    for name, col in (("Blue", ACCENT), ("Green", OK), ("Red", BAD)):
        style.configure(f"{name}.Horizontal.TProgressbar", troughcolor="#E3E7EE", background=col,
                        bordercolor="#E3E7EE", lightcolor=col, darkcolor=col, thickness=S(12 if compact else 16))

    style.configure("Vertical.TScrollbar", background="#334155", troughcolor="#0F172A", bordercolor="#0F172A",
                    arrowcolor="#94A3B8", lightcolor="#334155", darkcolor="#334155")
    q = queue.Queue()
    state = {"running": False, "start": 0.0, "stage": 0, "pct": 0, "auto": False, "ok": None}
    cd = {"n": 0, "job": None}
    step_states = ["pending"] * len(STEPS)   # pending / active / done / skipped / error

    # ---------- header ----------
    header = tk.Frame(root, bg=NAVY)
    header.pack(fill="x")
    hl = tk.Frame(header, bg=NAVY)
    hl.pack(side="left", padx=PAD, pady=S(10 if compact else 16))
    tk.Label(hl, text="SQL Backup Uploader", bg=NAVY, fg="white", font=(FONT, 18, "bold")).pack(anchor="w")
    if not compact:
        tk.Label(hl, text="Back up, verify and securely store your database in Amazon S3",
                 bg=NAVY, fg="#B6C0D8", font=(FONT, 10)).pack(anchor="w")
    if test_mode:
        tk.Label(header, text="  TEST MODE  ", bg=WARN, fg="white",
                 font=(FONT, 9, "bold"), pady=3).pack(side="right", padx=PAD)

    footer = tk.Frame(root, bg=BG)
    footer.pack(side="bottom", fill="x", padx=PAD, pady=(4, S(10 if compact else 18)))
    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True, padx=PAD, pady=(GAP, S(6)))

    def card(parent, **pack):
        f = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        f.pack(fill="x", **pack)
        return f

    # ---------- configuration summary ----------
    c1 = card(body, pady=(0, GAP))
    grid = tk.Frame(c1, bg=CARD)
    grid.pack(fill="x", padx=S(14 if compact else 18), pady=S(8 if compact else 12))
    dest = f"s3://{cfg['bucket_name']}/" + (cfg["s3_prefix"].strip("/") + "/" if cfg["s3_prefix"].strip("/") else "")
    info = [("DATABASE", cfg["database"]), ("SQL SERVER", "Dummy data (test)" if test_mode else cfg["sql_server"]),
            ("BACKUP FOLDER", cfg["backup_folder"]), ("DESTINATION", dest)]
    for i, (k, v) in enumerate(info):
        cell = tk.Frame(grid, bg=CARD)
        if i < 2:
            cell.grid(row=0, column=i, sticky="w", padx=(0, 30), pady=4)
        else:
            cell.grid(row=i - 1, column=0, columnspan=2, sticky="w", pady=4)
        tk.Label(cell, text=k, bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
        val = tk.Label(cell, text=v, bg=CARD, fg=TEXT, font=(FONT, 10), wraplength=w - S(110), justify="left")
        val.pack(anchor="w")
        wrap_labels.append(val)
    grid.columnconfigure(0, weight=1)
    grid.columnconfigure(1, weight=1)

    mode_row = tk.Frame(grid, bg=CARD)
    mode_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
    tk.Label(mode_row, text="START MODE", bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
    mode_var = tk.StringVar(value="auto" if autostart else "manual")
    for text, val in (("Manual  (wait for me to click Start)", "manual"),
                      ("Automatic  (start by itself when opened)", "auto")):
        tk.Radiobutton(mode_row, text=text, variable=mode_var, value=val, command=lambda: save_mode(),
                       bg=CARD, activebackground=CARD, fg=TEXT, selectcolor=CARD, font=(FONT, 10),
                       highlightthickness=0, bd=0, cursor="hand2").pack(side="left", padx=(0, 22), pady=(2, 0))

    # ---------- steps to run (checkboxes) ----------
    n_parts = int(cfg["part_count"])
    if test_mode:
        step_desc = {"backup": f"Backup  -  create a 30 KB dummy file split into {n_parts} parts",
                     "verify": f"Verify  -  SHA-256 of the {n_parts} joined parts",
                     "upload": "Upload  -  all parts + manifest to S3",
                     "archive": "Archive  -  older S3 folders to the cheap tier",
                     "cleanup": "Clean up  -  wait, then delete local backups and old logs"}
    else:
        step_desc = {"backup": f"Backup  -  compressed FULL backup into {n_parts} parts",
                     "verify": f"Verify  -  RESTORE VERIFYONLY checksum of the {n_parts} parts",
                     "upload": "Upload  -  all parts + manifest to S3",
                     "archive": "Archive  -  older S3 folders to the cheap tier",
                     "cleanup": "Clean up  -  wait, then delete local backups and old logs"}
    preselected = set(selected_steps or STEP_KEYS)
    step_vars = {k: tk.BooleanVar(value=k in preselected) for k in STEP_KEYS}
    c_steps = card(body, pady=(0, GAP))
    sg = tk.Frame(c_steps, bg=CARD)
    sg.pack(fill="x", padx=18, pady=(10, 10))
    sh = tk.Frame(sg, bg=CARD)
    sh.grid(row=0, column=0, columnspan=2, sticky="we")
    tk.Label(sh, text="STEPS TO RUN", bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(side="left")
    steps_hint = tk.Label(sh, text="", bg=CARD, fg=WARN, font=(FONT, 8, "bold"))
    steps_hint.pack(side="left", padx=(12, 0))
    links = []
    for txt, val in (("None", False), ("All", True)):
        lk = tk.Label(sh, text=txt, bg=CARD, fg=ACCENT, font=(FONT, 9, "underline"), cursor="hand2")
        lk.pack(side="right", padx=(10, 0))
        lk.bind("<Button-1>", lambda _e, v=val: set_all_steps(v))
        links.append(lk)
    step_checks = []
    for i, k in enumerate(STEP_KEYS):
        cb_ = tk.Checkbutton(sg, text=f"{i + 1}.  {step_desc[k]}", variable=step_vars[k],
                             command=lambda: on_steps_changed(), bg=CARD, activebackground=CARD, fg=TEXT,
                             selectcolor=CARD, font=(FONT, 9), highlightthickness=0, bd=0, anchor="w",
                             cursor="hand2")
        cb_.grid(row=1 + i // 2 if i < 4 else 3, column=i % 2 if i < 4 else 0,
                 columnspan=1 if i < 4 else 2, sticky="w", pady=1, padx=(0, 16))
        step_checks.append(cb_)
    sg.columnconfigure(0, weight=1)
    sg.columnconfigure(1, weight=1)

    # ---------- progress card ----------
    c2 = card(body, pady=(0, GAP))
    steps_canvas = tk.Canvas(c2, height=S(66 if compact else 74), bg=CARD, highlightthickness=0)
    steps_canvas.pack(fill="x", padx=18, pady=(12, 0))

    def draw_steps(_e=None):
        c = steps_canvas
        c.delete("all")
        width = c.winfo_width()
        if width < 100:
            width = w - S(110)
        n, margin, y, r = len(STEPS), S(60), S(22 if compact else 24), S(15 if compact else 16)
        gap = (width - 2 * margin) / (n - 1)
        for i in range(n - 1):
            col = OK if step_states[i] == "done" and step_states[i + 1] in ("done", "active") else BORDER
            c.create_line(margin + i * gap + r + 4, y, margin + (i + 1) * gap - r - 4, y, fill=col, width=3)
        for i, name in enumerate(STEPS):
            x, st = margin + i * gap, step_states[i]
            fill, outline, fg, sym = CARD, BORDER, MUTED, str(i + 1)
            if st == "done":
                fill, outline, fg, sym = OK, OK, "white", "\u2713"
            elif st == "active":
                fill, outline, fg = ACCENT, ACCENT, "white"
            elif st == "error":
                fill, outline, fg, sym = BAD, BAD, "white", "\u2715"
            elif st == "skipped":
                fill, outline, fg, sym = "#F3F4F6", BORDER, "#C0C5CE", "\u2013"
            c.create_oval(x - r, y - r, x + r, y + r, fill=fill, outline=outline, width=2,
                          dash=(3, 2) if st == "skipped" else None)
            c.create_text(x, y, text=sym, fill=fg, font=(FONT, 11, "bold"))
            c.create_text(x, y + r + S(14), text=name + (" (skipped)" if st == "skipped" else ""),
                          fill=MUTED if st in ("pending", "skipped") else TEXT,
                          font=(FONT, 9, "bold" if st == "active" else "normal"))

    steps_canvas.bind("<Configure>", draw_steps)

    pf = tk.Frame(c2, bg=CARD)
    pf.pack(fill="x", padx=18, pady=(4, 6))
    bar = ttk.Progressbar(pf, style="Blue.Horizontal.TProgressbar", mode="determinate", maximum=100)
    bar.pack(side="left", fill="x", expand=True)
    pct_lbl = tk.Label(pf, text="0%", bg=CARD, fg=TEXT, font=(FONT, 11, "bold"), width=5, anchor="e")
    pct_lbl.pack(side="left", padx=(10, 0))

    sf = tk.Frame(c2, bg=CARD)
    sf.pack(fill="x", padx=18, pady=(0, 14))
    status = tk.Label(sf, text="Ready. Click Start to begin.", bg=CARD, fg=MUTED, font=(FONT, 10), anchor="w")
    status.pack(side="left")
    elapsed = tk.Label(sf, text="", bg=CARD, fg=MUTED, font=(FONT, 10))
    elapsed.pack(side="right")

    # ---------- result banner (hidden until finished) ----------
    banner = tk.Frame(body, bg=OK)
    banner_title = tk.Label(banner, bg=OK, fg="white", font=(FONT, 11, "bold"), anchor="w")
    banner_title.pack(fill="x", padx=14, pady=(8, 0))
    banner_msg = tk.Label(banner, bg=OK, fg="white", font=(FONT, 9), anchor="w", justify="left", wraplength=w - S(110))
    wrap_labels.append(banner_msg)
    banner_msg.pack(fill="x", padx=14, pady=(0, 8))

    # ---------- activity log ----------
    log_head = tk.Frame(body, bg=BG)
    log_head.pack(fill="x")
    toggle = tk.Label(log_head, text="\u25BC  Activity log", bg=BG, fg=TEXT, font=(FONT, 10, "bold"), cursor="hand2")
    toggle.pack(side="left", pady=(0, 4))
    log_card = tk.Frame(body, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    log_card.pack(fill="both", expand=True)
    box = tk.Text(log_card, height=6 if compact else 10, bg="#0F172A", fg="#E2E8F0", font=("Consolas", 9), relief="flat",
                  padx=10, pady=8, wrap="word", state="disabled", borderwidth=0)
    sb = ttk.Scrollbar(log_card, orient="vertical", command=box.yview)
    box.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    box.pack(side="left", fill="both", expand=True)
    box.tag_config("warn", foreground="#FBBF24")
    box.tag_config("err", foreground="#F87171")
    box.tag_config("ok", foreground="#4ADE80")
    box.tag_config("time", foreground="#64748B")

    def toggle_log(_e=None):
        if log_card.winfo_ismapped():
            log_card.pack_forget()
            toggle.config(text="\u25B6  Activity log")
        else:
            log_card.pack(fill="both", expand=True)
            toggle.config(text="\u25BC  Activity log")

    toggle.bind("<Button-1>", toggle_log)

    def rewrap(e):   # long paths / messages wrap to the current window width instead of a fixed 700 px
        for lb in wrap_labels:
            lb.config(wraplength=max(S(200), e.width - S(40)))

    body.bind("<Configure>", rewrap)

    # ---------- buttons ----------
    def make_button(parent, text, kind, command):
        pal = {"primary": (ACCENT, ACCENT_DK, "white"), "secondary": ("#E5E8EE", "#D5DAE3", TEXT)}[kind]
        b = tk.Button(parent, text=text, command=command, bg=pal[0], fg=pal[2], activebackground=pal[1],
                      activeforeground=pal[2], relief="flat", bd=0, cursor="hand2", padx=22, pady=9,
                      font=(FONT, 10, "bold"), disabledforeground="#9CA3AF")
        b._pal = pal
        b.bind("<Enter>", lambda e: b["state"] == "normal" and b.config(bg=pal[1]))
        b.bind("<Leave>", lambda e: b["state"] == "normal" and b.config(bg=pal[0]))
        return b

    def open_logs():
        try:
            os.startfile(os.path.join(app_dir(), "Logs"))
        except Exception:
            messagebox.showinfo("Logs folder", os.path.join(app_dir(), "Logs"))

    btn_logs = make_button(footer, "Open Logs Folder", "secondary", open_logs)
    btn_logs.pack(side="left")
    start_text = "Start Test Run" if test_mode else "Start Backup"
    btn_start = make_button(footer, start_text, "primary", lambda: start())
    btn_start.pack(side="right")
    btn_close = make_button(footer, "Close", "secondary", lambda: on_close())
    btn_close.pack(side="right", padx=(0, 10))
    btn_cancel = make_button(footer, "Cancel Auto-Start", "secondary", lambda: cancel_countdown())

    # ---------- behaviour ----------
    def chosen_steps():
        return [k for k in STEP_KEYS if step_vars[k].get()]

    def reset_steps():
        """Before a run: ticked steps are 'pending', unticked ones 'skipped'."""
        chosen = chosen_steps()
        for i, k in enumerate(STEP_KEYS):
            step_states[i] = "pending" if k in chosen else "skipped"
        draw_steps()

    def set_step_state(key, st):
        step_states[STEP_KEYS.index(key)] = st
        draw_steps()

    def on_steps_changed():
        chosen = chosen_steps()
        if not state["running"]:
            reset_steps()
        if not chosen:
            steps_hint.config(text="nothing selected", fg=BAD)
        elif len(chosen) < len(STEP_KEYS):
            steps_hint.config(text=f"partial run: steps {', '.join(str(STEP_KEYS.index(k) + 1) for k in chosen)}",
                              fg=WARN)
        else:
            steps_hint.config(text="")
        if not state["running"] and not cd["job"]:
            btn_start.config(text=("Run Selected Steps" if 0 < len(chosen) < len(STEP_KEYS) else start_text))

    def set_all_steps(value):
        if state["running"]:
            return
        for v in step_vars.values():
            v.set(value)
        on_steps_changed()

    def lock_steps(locked):
        for cbx in step_checks:
            cbx.config(state="disabled" if locked else "normal")
        for lk in links:
            lk.config(fg=MUTED if locked else ACCENT, cursor="arrow" if locked else "hand2")

    def add_log(line):
        low = line.lower()
        tag = ("err" if ("failed" in low or "error" in low) else "warn" if "warning" in low
               else "ok" if ("succeeded" in low or "size-verified" in low or "complete" in low) else None)
        box.config(state="normal")
        stamp, _, rest = line.partition("  ")
        box.insert("end", stamp + "  ", "time")
        box.insert("end", rest + "\n", tag)
        box.see("end")
        box.config(state="disabled")

    def tick():
        if state["running"]:
            s = int(time.time() - state["start"])
            elapsed.config(text=f"Elapsed  {s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}")
            root.after(1000, tick)

    def show_banner(ok, title, msg):
        col = OK if ok else BAD
        for wdg in (banner, banner_title, banner_msg):
            wdg.config(bg=col)
        banner_title.config(text=title)
        banner_msg.config(text=msg)
        banner.pack(fill="x", pady=(0, 12), before=log_head)

    def worker(steps):
        p = Pipeline(cfg, logger, log_cb=lambda m: q.put(("log", m)),
                     progress_cb=lambda pct, s: q.put(("progress", pct, s)),
                     stage_cb=lambda k, st: q.put(("stage", k, st)),
                     test_mode=test_mode, date_override=date_override, steps=steps)
        ok, msg = p.run()
        q.put(("done", ok, msg))

    def start(auto=False):
        if state["running"]:
            return
        steps = chosen_steps()
        if not steps:
            if cd["job"]:
                cancel_countdown(message=False)
            status.config(text="Tick at least one step, then click Start.", fg=BAD)
            return
        if cd["job"]:
            cancel_countdown(message=False)
        btn_cancel.pack_forget()
        state.update(running=True, start=time.time(), stage=0, pct=0, auto=auto, steps=steps)
        banner.pack_forget()
        box.config(state="normal"); box.delete("1.0", "end"); box.config(state="disabled")
        bar.config(style="Blue.Horizontal.TProgressbar", value=0)
        pct_lbl.config(text="0%")
        status.config(text="Starting...", fg=TEXT)
        reset_steps()
        lock_steps(True)
        for b in (btn_start, btn_close):
            b.config(state="disabled", bg="#E5E8EE")
        btn_start.config(text="Running...")
        tick()
        threading.Thread(target=worker, args=(steps,), daemon=True).start()

    def finish(ok, msg):
        state["running"] = False
        state["ok"] = ok
        btn_start.config(state="normal", bg=ACCENT, text="Run Again")
        btn_close.config(state="normal", bg="#E5E8EE")
        lock_steps(False)
        partial = len(state.get("steps") or STEP_KEYS) < len(STEP_KEYS)
        if ok:
            bar.config(style="Green.Horizontal.TProgressbar", value=100)
            pct_lbl.config(text="100%")
            status.config(text="Completed successfully", fg=OK)
            show_banner(True, "\u2714  " + (f"Steps {', '.join(str(STEP_KEYS.index(k) + 1) for k in state['steps'])} "
                                             "completed successfully" if partial
                                             else "Backup completed successfully"), msg)
            secs = int(cfg.get("auto_close_seconds", 0) or 0)
            if state["auto"] and secs > 0:
                status.config(text=f"Completed successfully - closing in {secs} s", fg=OK)
                root.after(secs * 1000, root.destroy)
        else:
            bar.config(style="Red.Horizontal.TProgressbar")
            status.config(text="Failed - see the activity log", fg=BAD)
            show_banner(False, "\u2716  " + ("Step failed" if partial else "Backup failed"), f"{msg}\nDetails are saved in the Logs folder.")

    def poll():
        try:
            while True:
                item = q.get_nowait()
                if item[0] == "log":
                    add_log(item[1])
                elif item[0] == "progress":
                    state["pct"] = item[1]
                    bar["value"] = item[1]
                    pct_lbl.config(text=f"{int(item[1])}%")
                    status.config(text=item[2], fg=TEXT)
                elif item[0] == "stage":
                    set_step_state(item[1], item[2])
                else:
                    finish(item[1], item[2])
        except queue.Empty:
            pass
        root.after(150, poll)

    def on_close():
        if state["running"] and not messagebox.askyesno(
                "Backup in progress",
                "A backup is still running.\nIf you close now it will stop; running the program again "
                "will resume the upload.\n\nClose anyway?"):
            return
        root.destroy()

    def cancel_countdown(message=True):
        if cd["job"]:
            root.after_cancel(cd["job"])
            cd["job"] = None
        btn_cancel.pack_forget()
        chosen = chosen_steps()
        btn_start.config(text=("Run Selected Steps" if 0 < len(chosen) < len(STEP_KEYS) else start_text))
        if message and not state["running"]:
            status.config(text="Auto-start cancelled. Click Start when ready.", fg=MUTED)

    def countdown():
        if state["running"]:
            return
        if cd["n"] <= 0:
            cd["job"] = None
            start(auto=True)
            return
        status.config(text=f"Starting automatically in {cd['n']} s...", fg=ACCENT)
        cd["n"] -= 1
        cd["job"] = root.after(1000, countdown)

    def save_mode():
        mode = mode_var.get()
        if mode == "manual" and cd["job"]:
            cancel_countdown(message=False)
            status.config(text="Auto-start cancelled. Click Start when ready.", fg=MUTED)
        try:   # remember the choice for next time (start_mode is always stored in the local file)
            path = cfg.get("_path")
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["start_mode"] = mode
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=4)
            cfg["start_mode"] = mode
        except Exception as e:  # noqa
            messagebox.showwarning("Could not save", f"The setting works for now but could not be saved:\n{e}")

    root.protocol("WM_DELETE_WINDOW", on_close)
    on_steps_changed()
    poll()
    if autostart:
        cd["n"] = max(0, int(cfg.get("auto_start_delay_seconds", 5)))
        btn_start.config(text="Start Now")
        btn_cancel.pack(side="right", padx=(0, 10))
        root.after(400, countdown)
    root.mainloop()
    return state["ok"]


def show_result(title, text, ok=True):
    print(text)
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk(); r.withdraw()
        (messagebox.showinfo if ok else messagebox.showerror)(title, text)
        r.destroy()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true", help="run without a window")
    ap.add_argument("--autostart", action="store_true", help="open the window and start by itself")
    ap.add_argument("--manual", action="store_true", help="open the window and wait for the Start click")
    ap.add_argument("--check", action="store_true",
                    help="no backup: alert (SNS) if the newest complete backup in S3 is too old")
    ap.add_argument("--setup-bucket", action="store_true",
                    help="ADMIN, once: harden the bucket (block public access, encryption, HTTPS-only, "
                         "versioning, lifecycle rules)")
    ap.add_argument("--object-lock-days", type=int, default=0,
                    help="with --setup-bucket: also forbid deleting objects for N days (Object Lock, governance)")
    ap.add_argument("--no-versioning", action="store_true", help="with --setup-bucket: leave versioning as it is")
    ap.add_argument("--profile", help="AWS profile to use (e.g. an administrator profile for --setup-bucket)")
    ap.add_argument("--test", action="store_true",
                    help="TEST: skip MSSQL; use a 30 KB dummy file split into parts")
    ap.add_argument("--config", help="path to an alternative config file (e.g. config.test.json)")
    ap.add_argument("--date", help="work on the backup set of YYYY-MM-DD instead of today's. With --test: pretend "
                                   "today is that date (to create an 'older' folder). Without --test: only "
                                   "together with --steps that do not include 1 (e.g. verify/upload a set "
                                   "that was backed up before midnight)")
    ap.add_argument("--steps", help="run only these steps: numbers or names, comma-separated - "
                                    "1 backup, 2 verify, 3 upload, 4 archive, 5 cleanup (default: all). "
                                    "With a window, this only pre-ticks the checkboxes.")
    args = ap.parse_args()
    if exe_is_test_build():           # the *_TEST.exe always runs in test mode with its own config
        args.test = True
        args.config = args.config or os.path.join(app_dir(), "config.test.json")
    try:
        steps = parse_steps(args.steps)
    except ValueError as e:
        ap.error(str(e))
    date_override = None
    if args.date:
        if not args.test and (not args.steps or "backup" in steps):
            ap.error("--date needs --test, or --steps without step 1 (a real backup is always made for today)")
        date_override = datetime.strptime(args.date, "%Y-%m-%d")

    try:
        cfg = load_config(args.config, args.profile)
    except Exception as e:  # noqa
        if args.auto or args.check:
            print(f"Configuration error: {e}")
            sys.exit(2)
        show_result("Configuration error", str(e), ok=False)
        sys.exit(2)
    if args.profile:
        cfg["aws_profile"] = args.profile
    logger = setup_logging(cfg)

    if args.check:                    # monitoring only: needs S3 read + SNS, no SQL Server
        ok, _ = Pipeline(cfg, logger, test_mode=args.test).check_freshness()
        sys.exit(0 if ok else 1)

    if args.setup_bucket:
        results = Pipeline(cfg, logger, test_mode=args.test).setup_bucket(
            versioning=not args.no_versioning, lock_days=args.object_lock_days)
        text = f"Bucket setup for {cfg['bucket_name']}:\n" + "\n".join(
            ("  OK      " if ok else "  FAILED  ") + name for ok, name in results)
        all_ok = all(ok for ok, _ in results)
        logger.info(text)
        if args.auto:
            print(text)
        else:
            show_result("Bucket setup", text, ok=all_ok)
        sys.exit(0 if all_ok else 1)

    lock = acquire_single_instance_lock()
    if lock is None:
        logger.warning("Another instance is already running - exiting.")
        if not args.auto:
            show_result("Already running", "The backup utility is already running.", ok=False)
        sys.exit(3)

    if args.auto:
        ok, msg = Pipeline(cfg, logger, test_mode=args.test, date_override=date_override, steps=steps).run()
        print(("OK: " if ok else "FAILED: ") + msg)
        sys.exit(0 if ok else 1)
    autostart = args.autostart or (cfg["start_mode"] == "auto" and not args.manual)
    result = run_gui(cfg, logger, args.test, date_override, autostart, steps)
    if autostart and result is False:
        sys.exit(1)           # lets Task Scheduler see a failure even with a visible window


if __name__ == "__main__":
    main()