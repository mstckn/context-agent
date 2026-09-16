#!/usr/bin/env python3
"""
Agent Lifecycle Manager ve Proje Otomatik Keşif Sistemi

Context Agent'ın yaşam döngüsünü yönetir ve projeleri otomatik olarak algılar.
Özellikler:
- Proje otomatik keşif ve indeksleme
- IDE değişikliği algılama
- Çoklu proje desteği
- Otomatik bağlam yönetimi
- Session management
- Health monitoring

Kullanım:
  python lifecycle_manager.py --daemon
  python lifecycle_manager.py --status
  python lifecycle_manager.py --discover
"""

import os
import sys
import json
import time
import hashlib
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List, Set
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
import threading
import queue
import signal

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

class ProjectState(Enum):
    UNKNOWN = "unknown"
    INDEXING = "indexing"
    INDEXED = "indexed"
    WATCHING = "watching"
    STALE = "stale"
    ERROR = "error"

@dataclass
class ProjectInfo:
    path: Path
    name: str
    language: str
    framework: Optional[str]
    last_indexed: Optional[datetime]
    state: ProjectState
    file_count: int
    symbol_count: int
    size_mb: float
    has_tests: bool
    has_docker: bool
    has_git: bool
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.name,
            "language": self.language,
            "framework": self.framework,
            "last_indexed": self.last_indexed.isoformat() if self.last_indexed else None,
            "state": self.state.value,
            "file_count": self.file_count,
            "symbol_count": self.symbol_count,
            "size_mb": self.size_mb,
            "has_tests": self.has_tests,
            "has_docker": self.has_docker,
            "has_git": self.has_git,
        }

@dataclass
class ActiveSession:
    session_id: str
    project_path: Path
    workspace_path: Path
    started_at: datetime
    last_activity: datetime
    tool_calls: int
    context_items_count: int
    
    def is_active(self, timeout_minutes: int = 30) -> bool:
        return (datetime.now() - self.last_activity).total_seconds() < (timeout_minutes * 60)

class ProjectDetector:
    """Proje türlerini otomatik algılar"""
    
    FRAMEWORK_SIGNATURES = {
        "react": ["package.json", "src/App.jsx", "src/App.tsx"],
        "vue": ["package.json", "src/App.vue"],
        "nextjs": ["package.json", "next.config.js", "pages/", "app/"],
        "nuxt": ["package.json", "nuxt.config.js"],
        "django": ["manage.py", "requirements.txt"],
        "flask": ["app.py", "requirements.txt"],
        "fastapi": ["main.py", "requirements.txt"],
        "rails": ["Gemfile", "config/routes.rb"],
        "laravel": ["artisan", "composer.json"],
        "spring": ["pom.xml", "build.gradle"],
        "springboot": ["pom.xml", "application.properties"],
        "dotnet": ["*.csproj", "Program.cs"],
        "go": ["go.mod", "main.go"],
        "rust": ["Cargo.toml", "src/main.rs"],
        "python": ["requirements.txt", "setup.py", "pyproject.toml"],
        "nodejs": ["package.json"],
        "typescript": ["tsconfig.json"],
    }
    
    LANGUAGE_EXTENSIONS = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".jsx": "javascript",
        ".tsx": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".rb": "ruby",
        ".php": "php",
        ".cs": "csharp",
        ".cpp": "cpp",
        ".c": "c",
        ".swift": "swift",
        ".kt": "kotlin",
        ".scala": "scala",
        ".ex": "elixir",
        ".exs": "elixir",
        ".erl": "erlang",
    }
    
    def __init__(self, project_root: Path):
        self.project_root = project_root
    
    def detect_language(self) -> Optional[str]:
        """Projenin birincil dilini tespit et"""
        extensions: Dict[str, int] = {}
        
        for ext in self.LANGUAGE_EXTENSIONS.keys():
            count = sum(1 for _ in self.project_root.rglob(f"*{ext}"))
            if count > 0:
                extensions[self.LANGUAGE_EXTENSIONS[ext]] = count
        
        if extensions:
            return max(extensions, key=extensions.get)
        return None
    
    def detect_framework(self) -> Optional[str]:
        """Framework'ü tespit et"""
        for framework, signatures in self.FRAMEWORK_SIGNATURES.items():
            matches = 0
            for sig in signatures:
                if "/" in sig:
                    if (self.project_root / sig).exists():
                        matches += 1
                else:
                    if any((self.project_root).rglob(sig)):
                        matches += 1
            
            if matches >= 2:
                return framework
        
        return None
    
    def get_project_info(self) -> ProjectInfo:
        """Proje bilgilerini topla"""
        name = self.project_root.name
        
        file_count = sum(1 for _ in self.project_root.rglob("*") if _.is_file())
        
        size_mb = sum(
            f.stat().st_size for f in self.project_root.rglob("*") 
            if f.is_file()
        ) / (1024 * 1024)
        
        has_tests = any([
            (self.project_root / "tests").exists(),
            (self.project_root / "test").exists(),
            (self.project_root / "__tests__").exists(),
            list(self.project_root.rglob("*_test.py")),
            list(self.project_root.rglob("*.spec.*")),
        ])
        
        has_docker = any([
            (self.project_root / "Dockerfile").exists(),
            (self.project_root / "docker-compose.yml").exists(),
            (self.project_root / "docker-compose.yaml").exists(),
        ])
        
        has_git = (self.project_root / ".git").exists()
        
        context_dir = self.project_root / ".context"
        last_indexed = None
        symbol_count = 0
        
        if (context_dir / "map.json").exists():
            try:
                map_data = json.loads((context_dir / "map.json").read_text(encoding="utf-8"))
                if "symbols" in map_data:
                    symbol_count = len(map_data["symbols"])
                if "indexed_at" in map_data:
                    last_indexed = datetime.fromisoformat(map_data["indexed_at"])
            except Exception:
                pass
        
        state = ProjectState.UNKNOWN
        if last_indexed:
            if (datetime.now() - last_indexed).days > 7:
                state = ProjectState.STALE
            else:
                state = ProjectState.INDEXED
        if (context_dir / "watch_state.json").exists():
            state = ProjectState.WATCHING
        
        return ProjectInfo(
            path=self.project_root,
            name=name,
            language=self.detect_language() or "unknown",
            framework=self.detect_framework(),
            last_indexed=last_indexed,
            state=state,
            file_count=file_count,
            symbol_count=symbol_count,
            size_mb=round(size_mb, 2),
            has_tests=has_tests,
            has_docker=has_docker,
            has_git=has_git,
        )

class LifecycleManager:
    """Agent lifecycle ve proje yönetimi"""
    
    def __init__(self):
        self.home = Path.home()
        self.registry_path = self.home / ".context-agent" / "project_registry.json"
        self.state_file = self.home / ".context-agent" / "lifecycle_state.json"
        self.sessions: Dict[str, ActiveSession] = {}
        self.active_projects: Dict[str, ProjectInfo] = {}
        
        self._load_registry()
        self._load_state()
    
    def _load_registry(self):
        """Proje kayıtlarını yükle"""
        if self.registry_path.exists():
            try:
                data = json.loads(self.registry_path.read_text(encoding="utf-8"))
                for project_data in data.get("projects", []):
                    path = Path(project_data["path"])
                    if path.exists():
                        detector = ProjectDetector(path)
                        self.active_projects[str(path)] = detector.get_project_info()
            except Exception:
                pass
    
    def _save_registry(self):
        """Proje kayıtlarını kaydet"""
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "projects": [
                project.to_dict() for project in self.active_projects.values()
            ],
            "last_updated": datetime.now().isoformat()
        }
        
        self.registry_path.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8"
        )
    
    def _load_state(self):
        """Yaşam döngüsü durumunu yükle"""
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                sessions_data = data.get("sessions", {})
                
                for session_id, session_data in sessions_data.items():
                    self.sessions[session_id] = ActiveSession(
                        session_id=session_data["session_id"],
                        project_path=Path(session_data["project_path"]),
                        workspace_path=Path(session_data["workspace_path"]),
                        started_at=datetime.fromisoformat(session_data["started_at"]),
                        last_activity=datetime.fromisoformat(session_data["last_activity"]),
                        tool_calls=session_data.get("tool_calls", 0),
                        context_items_count=session_data.get("context_items_count", 0),
                    )
            except Exception:
                pass
    
    def _save_state(self):
        """Yaşam döngüsü durumunu kaydet"""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "sessions": {
                session_id: {
                    "session_id": session.session_id,
                    "project_path": str(session.project_path),
                    "workspace_path": str(session.workspace_path),
                    "started_at": session.started_at.isoformat(),
                    "last_activity": session.last_activity.isoformat(),
                    "tool_calls": session.tool_calls,
                    "context_items_count": session.context_items_count,
                }
                for session_id, session in self.sessions.items()
            },
            "last_updated": datetime.now().isoformat()
        }
        
        self.state_file.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8"
        )
    
    def discover_projects(self, search_paths: Optional[List[Path]] = None) -> List[ProjectInfo]:
        """Projeleri otomatik keşfet"""
        if search_paths is None:
            search_paths = [self.home]
        
        discovered = []
        project_markers = [
            ".git", ".context", "package.json", "pyproject.toml",
            "Cargo.toml", "go.mod", "pom.xml", "requirements.txt",
            "setup.py", "composer.json"
        ]
        
        for search_path in search_paths:
            if not search_path.exists():
                continue
            
            for item in search_path.iterdir():
                if not item.is_dir():
                    continue
                
                if item.name.startswith("."):
                    continue
                
                if any((item / marker).exists() for marker in project_markers):
                    if str(item) not in self.active_projects:
                        detector = ProjectDetector(item)
                        project_info = detector.get_project_info()
                        self.active_projects[str(item)] = project_info
                    
                    discovered.append(self.active_projects[str(item)])
        
        self._save_registry()
        return discovered
    
    def get_active_project(self, workspace_path: Optional[Path] = None) -> Optional[ProjectInfo]:
        """Aktif projeyi bul"""
        if workspace_path is None:
            workspace_path = Path.cwd()
        
        workspace_str = str(workspace_path.resolve())
        
        if workspace_str in self.active_projects:
            return self.active_projects[workspace_str]
        
        for parent in [workspace_path] + list(workspace_path.parents):
            parent_str = str(parent.resolve())
            if parent_str in self.active_projects:
                return self.active_projects[parent_str]
        
        detector = ProjectDetector(workspace_path)
        project_info = detector.get_project_info()
        self.active_projects[str(workspace_path)] = project_info
        self._save_registry()
        
        return project_info
    
    def create_session(
        self,
        project_path: Path,
        workspace_path: Optional[Path] = None
    ) -> ActiveSession:
        """Yeni oturum oluştur"""
        session_id = hashlib.sha256(
            f"{project_path}{time.time()}{os.getpid()}".encode()
        ).hexdigest()[:16]
        
        session = ActiveSession(
            session_id=session_id,
            project_path=project_path,
            workspace_path=workspace_path or project_path,
            started_at=datetime.now(),
            last_activity=datetime.now(),
            tool_calls=0,
            context_items_count=0,
        )
        
        self.sessions[session_id] = session
        self._save_state()
        
        return session
    
    def update_session(self, session_id: str, **kwargs):
        """Oturum bilgilerini güncelle"""
        if session_id in self.sessions:
            session = self.sessions[session_id]
            
            if "tool_calls" in kwargs:
                session.tool_calls += kwargs["tool_calls"]
            if "context_items_count" in kwargs:
                session.context_items_count = kwargs["context_items_count"]
            
            session.last_activity = datetime.now()
            self._save_state()
    
    def close_session(self, session_id: str):
        """Oturumu kapat"""
        if session_id in self.sessions:
            del self.sessions[session_id]
            self._save_state()
    
    def cleanup_stale_sessions(self):
        """Zaman aşımına uğrayan oturumları temizle"""
        stale = [
            session_id for session_id, session in self.sessions.items()
            if not session.is_active()
        ]
        
        for session_id in stale:
            self.close_session(session_id)
        
        return len(stale)
    
    def get_status(self) -> Dict[str, Any]:
        """Sistem durumunu döndür"""
        self.cleanup_stale_sessions()
        
        return {
            "active_projects": len(self.active_projects),
            "active_sessions": len(self.sessions),
            "projects": [
                project.to_dict() for project in self.active_projects.values()
            ],
            "sessions": [
                {
                    "session_id": s.session_id,
                    "project": s.project_path.name,
                    "started_at": s.started_at.isoformat(),
                    "last_activity": s.last_activity.isoformat(),
                    "tool_calls": s.tool_calls,
                    "is_active": s.is_active(),
                }
                for s in self.sessions.values()
            ],
            "timestamp": datetime.now().isoformat(),
        }
    
    def index_project(self, project_path: Path, force: bool = False) -> bool:
        """Projeyi indeksle"""
        context_dir = project_path / ".context"
        map_file = context_dir / "map.json"
        
        if map_file.exists() and not force:
            try:
                data = json.loads(map_file.read_text(encoding="utf-8"))
                if "indexed_at" in data:
                    last_indexed = datetime.fromisoformat(data["indexed_at"])
                    if (datetime.now() - last_indexed).days < 1:
                        return True
            except Exception:
                pass
        
        index_script = self._find_index_script(project_path)
        if not index_script:
            return False
        
        try:
            result = subprocess.run(
                [sys.executable, str(index_script)],
                cwd=str(project_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300
            )
            
            if project_path in self.active_projects:
                self.active_projects[str(project_path)].state = ProjectState.INDEXED
                self.active_projects[str(project_path)].last_indexed = datetime.now()
            
            self._save_registry()
            return result.returncode == 0
        
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False
    
    def _find_index_script(self, project_path: Path) -> Optional[Path]:
        """Index script'ini bul"""
        index_path = project_path / ".context" / "scripts" / "index.py"
        if index_path.exists():
            return index_path
        
        global_index = Path.home() / ".context-agent" / "scripts" / "index.py"
        if global_index.exists():
            return global_index
        
        return None

class LifecycleDaemon:
    """Arka planda çalışan daemon"""
    
    def __init__(self, check_interval: int = 60):
        self.manager = LifecycleManager()
        self.check_interval = check_interval
        self.running = False
        self.thread = None
    
    def start(self):
        """Daemon'u başlat"""
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
    
    def stop(self):
        """Daemon'u durdur"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
    
    def _run(self):
        """Ana döngü"""
        while self.running:
            try:
                self._check_active_project()
                self.manager.cleanup_stale_sessions()
                self._update_registry()
            except Exception:
                pass
            
            time.sleep(self.check_interval)
    
    def _check_active_project(self):
        """Aktif projeyi kontrol et"""
        workspace = Path.cwd()
        project = self.manager.get_active_project(workspace)
        
        if project and project.state in [ProjectState.STALE, ProjectState.UNKNOWN]:
            self.manager.index_project(project.path)
    
    def _update_registry(self):
        """Kayıtları güncelle"""
        for project_path_str, project in self.manager.active_projects.items():
            project_path = Path(project_path_str)
            if project_path.exists():
                detector = ProjectDetector(project_path)
                updated = detector.get_project_info()
                self.manager.active_projects[project_path_str] = updated
        
        self.manager._save_registry()

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Context Agent Lifecycle Manager")
    parser.add_argument("--daemon", action="store_true", help="Run as daemon")
    parser.add_argument("--status", action="store_true", help="Show status")
    parser.add_argument("--discover", action="store_true", help="Discover projects")
    parser.add_argument("--index", type=Path, help="Index a project")
    parser.add_argument("--interval", type=int, default=60, help="Check interval (seconds)")
    
    args = parser.parse_args()
    
    manager = LifecycleManager()
    
    if args.status:
        status = manager.get_status()
        print(json.dumps(status, indent=2))
    
    elif args.discover:
        projects = manager.discover_projects()
        print(f"Discovered {len(projects)} projects:")
        for project in projects:
            print(f"  - {project.name} ({project.path}) [{project.state.value}]")
    
    elif args.index:
        success = manager.index_project(args.index)
        print(f"Indexing {'succeeded' if success else 'failed'}")
    
    elif args.daemon:
        print("Starting Context Agent Lifecycle Daemon...")
        daemon = LifecycleDaemon(check_interval=args.interval)
        daemon.start()
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping daemon...")
            daemon.stop()
    
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
