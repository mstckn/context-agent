#!/usr/bin/env python3
"""
IDE Otomatik Algılama Modülü

Tüm popüler IDE'leri otomatik olarak tespit eder:
- VSCode
- JetBrains (PyCharm, IntelliJ, WebStorm, etc.)
- Neovim
- Cursor
- Windsurf
- GitHub Copilot Chat
- Trae IDE
- Visual Studio
"""

import os
import sys
import json
import shutil
import subprocess
import platform
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, asdict
from enum import Enum

class IDEType(Enum):
    VSCODE = "vscode"
    JETBRAINS = "jetbrains"
    NEOVIM = "neovim"
    CURSOR = "cursor"
    WINDSURF = "windsurf"
    GITHUB_COPILOT = "github_copilot"
    TRAE = "trae"
    VISUAL_STUDIO = "visual_studio"
    UNKNOWN = "unknown"

@dataclass
class IDEDetection:
    ide_type: IDEType
    name: str
    version: Optional[str]
    workspace_path: Optional[Path]
    config_dir: Optional[Path]
    extensions: List[Any]
    supports_mcp: bool
    mcp_config_location: Optional[Path]
    detection_method: str
    mcp_plugins: List[Any] = None
    
    def __post_init__(self):
        if self.mcp_plugins is None:
            self.mcp_plugins = []

class IDEDetector:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._detected = None
        return cls._instance
    
    def __init__(self):
        if self._detected is None:
            self._detected = self._detect()
    
    def _detect(self) -> IDEDetection:
        """IDE'yi tespit et"""
        detection_methods = [
            self._detect_from_environment,
            self._detect_from_process,
            self._detect_from_config,
        ]
        
        for method in detection_methods:
            result = method()
            if result and result.ide_type != IDEType.UNKNOWN:
                return result
        
        return IDEDetection(
            ide_type=IDEType.UNKNOWN,
            name="Unknown",
            version=None,
            workspace_path=None,
            config_dir=None,
            extensions=[],
            supports_mcp=True,
            mcp_config_location=None,
            detection_method="unknown"
        )
    
    def _detect_from_environment(self) -> Optional[IDEDetection]:
        """Ortam değişkenlerinden IDE tespiti"""
        
        # VSCode
        if os.getenv("VSCODE_GIT_ASKPASS") or os.getenv("VSCODE_INJECTION"):
            return self._create_vscode_detection("environment")
        
        # Cursor
        if os.getenv("CURSOR_SETTINGS"):
            return self._create_cursor_detection("environment")
        
        # JetBrains
        if os.getenv("JETBRAINS_PRODUCT_VERSION") or os.getenv("IDEA_INITIAL_DIRECTORY"):
            return self._create_jetbrains_detection("environment")
        
        # Neovim
        if os.getenv("NVIM") or os.getenv("MYVIMRC"):
            return self._create_neovim_detection("environment")
        
        # Windsurf
        if os.getenv("WINDSURF"):
            return self._create_windsurf_detection("environment")
        
        # Trae
        if os.getenv("TRAE_IDE"):
            return self._create_trae_detection("environment")
        
        # GitHub Copilot
        if os.getenv("GITHUB_COPILOT"):
            return self._create_copilot_detection("environment")
        
        return None
    
    def _detect_from_process(self) -> Optional[IDEDetection]:
        """Aktif process'lerden IDE tespiti"""
        try:
            if platform.system() == "Windows":
                result = subprocess.run(
                    ["powershell", "-Command", 
                     "Get-Process | Where-Object {$_.ProcessName -match 'code|cursor|idea|pycharm|goland|webstorm|vim|nvim|windsurf|trae|devhub'}"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                processes = result.stdout.lower()
            else:
                result = subprocess.run(
                    ["ps", "aux"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                processes = result.stdout.lower()
            
            # IDE process eşleşmeleri
            process_map = {
                "code": (IDEType.VSCODE, "VSCode"),
                "cursor": (IDEType.CURSOR, "Cursor"),
                "idea": (IDEType.JETBRAINS, "IntelliJ IDEA"),
                "pycharm": (IDEType.JETBRAINS, "PyCharm"),
                "goland": (IDEType.JETBRAINS, "GoLand"),
                "webstorm": (IDEType.JETBRAINS, "WebStorm"),
                "phpstorm": (IDEType.JETBRAINS, "PHPStorm"),
                "rider": (IDEType.JETBRAINS, "Rider"),
                "clion": (IDEType.JETBRAINS, "CLion"),
                "ruby": (IDEType.JETBRAINS, "RubyMine"),
                "aws": (IDEType.JETBRAINS, "AWS Toolkit"),
                "vim": (IDEType.NEOVIM, "Vim"),
                "nvim": (IDEType.NEOVIM, "Neovim"),
                "windsurf": (IDEType.WINDSURF, "Windsurf"),
                "trae": (IDEType.TRAE, "Trae IDE"),
            }
            
            for keyword, (ide_type, name) in process_map.items():
                if keyword in processes:
                    return IDEDetection(
                        ide_type=ide_type,
                        name=name,
                        version=None,
                        workspace_path=None,
                        config_dir=None,
                        extensions=[],
                        supports_mcp=True,
                        mcp_config_location=self._get_mcp_config_location(ide_type),
                        detection_method="process"
                    )
        except Exception:
            pass
        
        return None
    
    def _detect_from_config(self) -> Optional[IDEDetection]:
        """Konfigürasyon dosyalarından IDE tespiti"""
        home = Path.home()
        
        config_checks = [
            (home / ".vscode" / "extensions", IDEType.VSCODE, "VSCode", "config"),
            (home / ".cursor" / "extensions", IDEType.CURSOR, "Cursor", "config"),
            (home / ".config" / "Code", IDEType.VSCODE, "VSCode", "config"),
            (home / ".config" / "Cursor", IDEType.CURSOR, "Cursor", "config"),
            (home / ".config" / "Neovim", IDEType.NEOVIM, "Neovim", "config"),
            (home / ".config" / "nvim", IDEType.NEOVIM, "Neovim", "config"),
        ]
        
        for config_path, ide_type, name, method in config_checks:
            if config_path.exists():
                return IDEDetection(
                    ide_type=ide_type,
                    name=name,
                    version=None,
                    workspace_path=None,
                    config_dir=config_path,
                    extensions=self._get_installed_extensions(ide_type, config_path),
                    supports_mcp=True,
                    mcp_config_location=self._get_mcp_config_location(ide_type),
                    detection_method=f"config:{method}"
                )
        
        return None
    
    def _get_installed_extensions(self, ide_type: IDEType, config_dir: Path) -> List[str]:
        """Yüklü eklentileri listele"""
        extensions = []
        
        if ide_type == IDEType.VSCODE:
            extensions = self._get_vscode_extensions()
        elif ide_type == IDEType.CURSOR:
            extensions = self._get_cursor_extensions()
        elif ide_type == IDEType.JETBRAINS:
            extensions = self._get_jetbrains_extensions(config_dir)
        elif ide_type == IDEType.NEOVIM:
            extensions = self._get_neovim_plugins(config_dir)
        elif ide_type == IDEType.WINDSURF:
            extensions = self._get_windsurf_extensions()
        elif ide_type == IDEType.TRAE:
            extensions = self._get_trae_extensions()
        
        return extensions
    
    def _get_vscode_extensions(self) -> List[Dict[str, str]]:
        """VSCode eklentilerini detaylı al"""
        extensions = []
        home = Path.home()
        
        ext_locations = [
            home / ".vscode" / "extensions",
            home / ".config" / "Code" / "extensions",
            home / ".cursor" / "extensions",
        ]
        
        for ext_dir in ext_locations:
            if not ext_dir.exists():
                continue
            
            for ext in ext_dir.iterdir():
                if not ext.is_dir():
                    continue
                
                ext_id = ext.name
                display_name = ext_id
                version = None
                
                package_json = ext / "extension.vsixmanifest" if ext.suffix == ".vsix" else ext / "package.json"
                
                if package_json.exists():
                    try:
                        import json
                        pkg_data = json.loads(package_json.read_text(encoding="utf-8"))
                        display_name = pkg_data.get("displayName", display_name)
                        version = pkg_data.get("version", version)
                    except Exception:
                        pass
                
                extensions.append({
                    "id": ext_id,
                    "name": display_name,
                    "version": version,
                    "path": str(ext),
                    "type": "marketplace" if ext_id.startswith("ms-") else "custom"
                })
        
        return extensions
    
    def _get_cursor_extensions(self) -> List[Dict[str, str]]:
        """Cursor eklentilerini detaylı al"""
        extensions = []
        cursor_ext_dir = Path.home() / ".cursor" / "extensions"
        
        if cursor_ext_dir.exists():
            for ext in cursor_ext_dir.iterdir():
                if not ext.is_dir():
                    continue
                
                package_json = ext / "package.json"
                ext_id = ext.name
                display_name = ext_id
                version = None
                
                if package_json.exists():
                    try:
                        import json
                        pkg_data = json.loads(package_json.read_text(encoding="utf-8"))
                        display_name = pkg_data.get("displayName", display_name)
                        version = pkg_data.get("version", version)
                    except Exception:
                        pass
                
                extensions.append({
                    "id": ext_id,
                    "name": display_name,
                    "version": version,
                    "path": str(ext),
                    "type": "custom"
                })
        
        return extensions
    
    def _get_jetbrains_extensions(self, config_dir: Path) -> List[Dict[str, str]]:
        """JetBrains eklentilerini detaylı al"""
        extensions = []
        home = Path.home()
        
        plugin_dirs = [
            home / ".local" / "share" / "JetBrains" / "intellij-plugins",
            home / ".local" / "share" / "JetBrains" / "PyCharm" / "plugins",
            home / ".local" / "share" / "JetBrains" / "WebStorm" / "plugins",
            home / ".config" / "JetBrains" / "plugins",
            config_dir / "plugins",
        ]
        
        for plugin_dir in plugin_dirs:
            if not plugin_dir.exists():
                continue
            
            for plugin in plugin_dir.iterdir():
                if not plugin.is_dir():
                    continue
                
                plugin_id = plugin.name
                display_name = plugin_id
                version = None
                description = None
                
                plugin_xml = plugin / "plugin.xml"
                build_gradle = plugin / "build.gradle"
                plugin_json = plugin / "plugin.json"
                
                if plugin_xml.exists():
                    try:
                        content = plugin_xml.read_text(encoding="utf-8", errors="ignore")
                        import re
                        name_match = re.search(r'<name>(.*?)</name>', content)
                        ver_match = re.search(r'<version>(.*?)</version>', content)
                        desc_match = re.search(r'<description>(.*?)</description>', content, re.DOTALL)
                        
                        if name_match:
                            display_name = name_match.group(1).strip()
                        if ver_match:
                            version = ver_match.group(1).strip()
                        if desc_match:
                            description = desc_match.group(1).strip()[:200]
                    except Exception:
                        pass
                
                elif plugin_json.exists():
                    try:
                        import json
                        pkg_data = json.loads(plugin_json.read_text(encoding="utf-8"))
                        display_name = pkg_data.get("name", display_name)
                        version = pkg_data.get("version", version)
                        description = pkg_data.get("description", description)
                    except Exception:
                        pass
                
                extensions.append({
                    "id": plugin_id,
                    "name": display_name,
                    "version": version,
                    "description": description,
                    "path": str(plugin),
                    "type": "bundled"
                })
        
        return extensions
    
    def _get_neovim_plugins(self, config_dir: Path) -> List[Dict[str, str]]:
        """Neovim plugin'lerini detaylı al"""
        plugins = []
        home = Path.home()
        
        plugin_locations = [
            config_dir / "plugged",
            config_dir / "start",
            config_dir / "opt",
            home / ".local" / "share" / "nvim" / "site" / "pack" / "nvim" / "start",
            home / ".local" / "share" / "nvim" / "site" / "pack" / "nvim" / "opt",
        ]
        
        for plugin_dir in plugin_locations:
            if not plugin_dir.exists():
                continue
            
            for plugin in plugin_dir.iterdir():
                if not plugin.is_dir():
                    continue
                
                plugin_id = plugin.name
                display_name = plugin_id
                description = None
                
                init_lua = plugin / "init.lua"
                plugin_lua = plugin / "plugin" / "init.lua"
                readme = None
                
                for readme_name in ["README.md", "readme.md", "README.txt"]:
                    readme_candidate = plugin / readme_name
                    if readme_candidate.exists():
                        readme = readme_candidate
                        break
                
                if readme:
                    try:
                        content = readme.read_text(encoding="utf-8", errors="ignore")
                        description = content.split('\n')[0].strip()
                        if description.startswith("#"):
                            description = description.lstrip("# ").strip()
                        description = description[:200]
                    except Exception:
                        pass
                
                plugin_type = "start"
                if plugin_dir.name == "opt" or "opt" in str(plugin_dir):
                    plugin_type = "optional"
                
                plugins.append({
                    "id": plugin_id,
                    "name": display_name,
                    "description": description,
                    "path": str(plugin),
                    "type": plugin_type,
                    "has_init": (init_lua.exists() or plugin_lua.exists())
                })
        
        return plugins
    
    def _get_windsurf_extensions(self) -> List[Dict[str, str]]:
        """Windsurf eklentilerini detaylı al"""
        extensions = []
        windsurf_ext_dir = Path.home() / ".windsurf" / "extensions"
        
        if windsurf_ext_dir.exists():
            for ext in windsurf_ext_dir.iterdir():
                if not ext.is_dir():
                    continue
                
                package_json = ext / "package.json"
                ext_id = ext.name
                display_name = ext_id
                version = None
                
                if package_json.exists():
                    try:
                        import json
                        pkg_data = json.loads(package_json.read_text(encoding="utf-8"))
                        display_name = pkg_data.get("displayName", display_name)
                        version = pkg_data.get("version", version)
                    except Exception:
                        pass
                
                extensions.append({
                    "id": ext_id,
                    "name": display_name,
                    "version": version,
                    "path": str(ext),
                    "type": "custom"
                })
        
        return extensions
    
    def _get_trae_extensions(self) -> List[Dict[str, str]]:
        """Trae IDE eklentilerini detaylı al"""
        extensions = []
        trae_ext_dir = Path.home() / ".trae" / "extensions"
        
        if trae_ext_dir.exists():
            for ext in trae_ext_dir.iterdir():
                if not ext.is_dir():
                    continue
                
                package_json = ext / "package.json"
                ext_id = ext.name
                display_name = ext_id
                version = None
                
                if package_json.exists():
                    try:
                        import json
                        pkg_data = json.loads(package_json.read_text(encoding="utf-8"))
                        display_name = pkg_data.get("displayName", display_name)
                        version = pkg_data.get("version", version)
                    except Exception:
                        pass
                
                extensions.append({
                    "id": ext_id,
                    "name": display_name,
                    "version": version,
                    "path": str(ext),
                    "type": "custom"
                })
        
        return extensions
    
    def _get_mcp_config_location(self, ide_type: IDEType) -> Optional[Path]:
        """MCP konfigürasyon dosyası yolu"""
        home = Path.home()
        
        locations = {
            IDEType.VSCODE: home / ".config" / "Code" / "User" / "settings.json",
            IDEType.CURSOR: home / ".cursor" / "settings.json",
            IDEType.JETBRAINS: home / ".jetbrains" / "config" / "options" / "mcp.json",
            IDEType.NEOVIM: home / ".config" / "nvim" / "mcp_config.json",
            IDEType.WINDSURF: home / ".windsurf" / "settings.json",
            IDEType.TRAE: home / ".trae" / "settings.json",
        }
        
        return locations.get(ide_type)
    
    def _create_vscode_detection(self, method: str) -> IDEDetection:
        return IDEDetection(
            ide_type=IDEType.VSCODE,
            name="Visual Studio Code",
            version=self._get_vscode_version(),
            workspace_path=self._get_vscode_workspace(),
            config_dir=Path.home() / ".config" / "Code",
            extensions=self._get_vscode_extensions(),
            supports_mcp=True,
            mcp_config_location=Path.home() / ".config" / "Code" / "User" / "settings.json",
            detection_method=method
        )
    
    def _create_cursor_detection(self, method: str) -> IDEDetection:
        return IDEDetection(
            ide_type=IDEType.CURSOR,
            name="Cursor",
            version=self._get_cursor_version(),
            workspace_path=self._get_vscode_workspace(),
            config_dir=Path.home() / ".cursor",
            extensions=self._get_vscode_extensions(),
            supports_mcp=True,
            mcp_config_location=Path.home() / ".cursor" / "settings.json",
            detection_method=method
        )
    
    def _create_jetbrains_detection(self, method: str) -> IDEDetection:
        product = os.getenv("JETBRAINS_PRODUCT", "IntelliJ IDEA")
        version = os.getenv("JETBRAINS_PRODUCT_VERSION")
        
        return IDEDetection(
            ide_type=IDEType.JETBRAINS,
            name=product,
            version=version,
            workspace_path=self._get_jetbrains_workspace(),
            config_dir=Path(os.getenv("IDEA_CONFIG_PATH", str(Path.home() / ".JetBrains"))),
            extensions=[],
            supports_mcp=True,
            mcp_config_location=Path.home() / ".jetbrains" / "config" / "options" / "mcp.json",
            detection_method=method
        )
    
    def _create_neovim_detection(self, method: str) -> IDEDetection:
        nvim_path = shutil.which("nvim")
        version = (
            subprocess.run(
                [nvim_path, "--version"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.split("\n")[0]
            if nvim_path
            else None
        )
        
        return IDEDetection(
            ide_type=IDEType.NEOVIM,
            name="Neovim",
            version=version,
            workspace_path=Path.cwd(),
            config_dir=Path.home() / ".config" / "nvim",
            extensions=[],
            supports_mcp=True,
            mcp_config_location=Path.home() / ".config" / "nvim" / "mcp_config.json",
            detection_method=method
        )
    
    def _create_windsurf_detection(self, method: str) -> IDEDetection:
        return IDEDetection(
            ide_type=IDEType.WINDSURF,
            name="Windsurf",
            version=None,
            workspace_path=None,
            config_dir=Path.home() / ".windsurf",
            extensions=[],
            supports_mcp=True,
            mcp_config_location=Path.home() / ".windsurf" / "settings.json",
            detection_method=method
        )
    
    def _create_trae_detection(self, method: str) -> IDEDetection:
        return IDEDetection(
            ide_type=IDEType.TRAE,
            name="Trae IDE",
            version=None,
            workspace_path=Path.cwd(),
            config_dir=Path.home() / ".trae",
            extensions=[],
            supports_mcp=True,
            mcp_config_location=Path.home() / ".trae" / "settings.json",
            detection_method=method
        )
    
    def _create_copilot_detection(self, method: str) -> IDEDetection:
        return IDEDetection(
            ide_type=IDEType.GITHUB_COPILOT,
            name="GitHub Copilot",
            version=os.getenv("GITHUB_COPILOT_VERSION"),
            workspace_path=None,
            config_dir=Path.home() / ".config" / "github-copilot",
            extensions=[],
            supports_mcp=True,
            mcp_config_location=None,
            detection_method=method
        )
    
    def _get_vscode_version(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["code", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return result.stdout.split('\n')[0]
        except Exception:
            pass
        return None
    
    def _get_cursor_version(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["cursor", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None
    
    def _get_vscode_workspace(self) -> Optional[Path]:
        env_workspace = os.getenv("VSCODE_WORKSPACE_FOLDER")
        if env_workspace:
            return Path(env_workspace)
        
        vscode_ipc = os.getenv("VSCODE_IPC_HOOK")
        if vscode_ipc:
            parts = vscode_ipc.split('/')
            for i, part in enumerate(parts):
                if part == 'file' and i + 1 < len(parts):
                    return Path(parts[i + 1]).parent
        return None
    
    def _get_jetbrains_workspace(self) -> Optional[Path]:
        project_path = os.getenv("IDEA_PROJECT_PATH")
        if project_path:
            return Path(project_path)
        
        initial_dir = os.getenv("IDEA_INITIAL_DIRECTORY")
        if initial_dir:
            return Path(initial_dir)
        
        return None
    
    def _get_vscode_extensions(self) -> List[str]:
        extensions = []
        ext_dir = Path.home() / ".config" / "Code" / "extensions"
        
        if ext_dir.exists():
            for ext in ext_dir.iterdir():
                if ext.is_directory() and ext.name.startswith("ms-"):
                    extensions.append(ext.name)
        
        return extensions
    
    @property
    def detected(self) -> IDEDetection:
        """Algılanan IDE bilgisi"""
        return self._detected
    
    def is_supported(self) -> bool:
        """IDE MCP'i destekliyor mu?"""
        return self._detected.ide_type != IDEType.UNKNOWN and self._detected.supports_mcp
    
    def get_mcp_config_template(self) -> Dict[str, Any]:
        """IDE'ye özel MCP konfigürasyon şablonu"""
        config = {
            "mcpServers": {
                "context-agent": {
                    "command": "python",
                    "args": [
                        str(Path.home() / ".context-agent" / "launcher.py")
                    ]
                }
            }
        }
        
        if self._detected.ide_type == IDEType.VISUAL_STUDIO:
            config = {
                "mcpServers": {
                    "context-agent": {
                        "command": "python",
                        "args": [
                            str(Path.home() / ".context-agent" / "launcher.py")
                        ],
                        "env": {
                            "CONTEXT_AGENT_IDE": "visual_studio"
                        }
                    }
                }
            }
        
        return config
    
    def get_workspace_path(self) -> Optional[Path]:
        """Aktif çalışma alanı yolu"""
        return self._detected.workspace_path
    
    def _detect_mcp_plugins(self, extensions: List[Any]) -> List[Dict[str, Any]]:
        """MCP ile ilgili eklentileri tespit et"""
        known_mcp_plugins = {
            # VSCode Marketplace Extensions
            "ms-toolsai.jupyter": {"name": "Jupyter", "category": "ai_ml", "capabilities": ["notebook", "interactive"]},
            "ms-toolsai.vscode-ai": {"name": "Azure Machine Learning", "category": "cloud_ml", "capabilities": ["azure_ml"]},
            "ms-python.python": {"name": "Python", "category": "language", "capabilities": ["python", "linting"]},
            "ms-vscode.powershell": {"name": "PowerShell", "category": "language", "capabilities": ["powershell"]},
            
            # AI/ML Related
            "github.copilot": {"name": "GitHub Copilot", "category": "ai_code_completion", "capabilities": ["ai_completion", "mcp"]},
            "github.copilot-nightly": {"name": "GitHub Copilot Nightly", "category": "ai_code_completion", "capabilities": ["ai_completion", "mcp"]},
            "anthropic.claude-code": {"name": "Claude for Code", "category": "ai_assistant", "capabilities": ["claude", "ai_chat", "mcp"]},
            "openai.gpt": {"name": "OpenAI GPT", "category": "ai_chat", "capabilities": ["openai", "gpt"]},
            "cursor-ai.cursor": {"name": "Cursor AI", "category": "ai_editor", "capabilities": ["cursor", "ai"]},
            "windsurf.windsurf": {"name": "Windsurf", "category": "ai_editor", "capabilities": ["windsurf", "ai"]},
            "trae.trae": {"name": "Trae IDE", "category": "ai_editor", "capabilities": ["trae", "ai"]},
            "anthropicanthropic.claude-code": {"name": "Claude Code", "category": "ai_assistant", "capabilities": ["claude", "cli"]},
            
            # MCP Related
            "modelcontextprotocol.server": {"name": "MCP Server", "category": "mcp", "capabilities": ["mcp_protocol"]},
            "vscode-mcp": {"name": "VSCode MCP", "category": "mcp", "capabilities": ["mcp_protocol"]},
            "copilot-mcp": {"name": "Copilot MCP", "category": "mcp", "capabilities": ["copilot", "mcp"]},
            
            # Code Generation
            "tabnine.tabnine-vscode": {"name": "Tabnine", "category": "ai_completion", "capabilities": ["ai_completion", "tabnine"]},
            "codestream.codestream": {"name": "CodeStream", "category": "collaboration", "capabilities": ["code_sharing"]},
            "amazonwebservices.aws-toolkit-vscode": {"name": "AWS Toolkit", "category": "cloud", "capabilities": ["aws", "cloudformation"]},
            "ms-azuretools.vscode-docker": {"name": "Docker", "category": "container", "capabilities": ["docker", "containers"]},
            
            # Development Tools
            "ms-vscode-remote.remote-containers": {"name": "Remote Containers", "category": "remote", "capabilities": ["containers", "devops"]},
            "ms-vscode-remote.remote-ssh": {"name": "Remote SSH", "category": "remote", "capabilities": ["ssh", "remote"]},
            "ms-vscode-remote.vscode-remote-extensionpack": {"name": "Remote Development", "category": "remote", "capabilities": ["remote"]},
            
            # Language Server
            "rust-lang.rust-analyzer": {"name": "rust-analyzer", "category": "language_server", "capabilities": ["rust", "lsp"]},
            "golang.go": {"name": "Go", "category": "language_server", "capabilities": ["go", "lsp"]},
            "ms-python.python": {"name": "Python", "category": "language_server", "capabilities": ["python", "lsp"]},
            "vscjava.vscode-java-debug": {"name": "Java Debug", "category": "language_server", "capabilities": ["java", "debug"]},
            "redhat.java": {"name": "Language Support for Java", "category": "language_server", "capabilities": ["java", "lsp"]},
            "ms-vscode.cpptools": {"name": "C/C++", "category": "language_server", "capabilities": ["cpp", "c", "lsp"]},
            
            # Testing & Quality
            "ms-python.testing": {"name": "Python Testing", "category": "testing", "capabilities": ["pytest", "testing"]},
            "hbenl.vscode-test-explorer": {"name": "Test Explorer", "category": "testing", "capabilities": ["testing", "test_runner"]},
            
            # Documentation & AI
            "stackprinter.vscode": {"name": "Stack Printer", "category": "documentation", "capabilities": ["stackoverflow"]},
            "ms-vscode.vscode-typescript-next": {"name": "TypeScript", "category": "language", "capabilities": ["typescript", "lsp"]},
        }
        
        mcp_keywords = [
            "mcp", "model context protocol", "copilot", "github copilot",
            "context agent", "claude", "openai", "anthropic",
            "cursor", "windsurf", "trae", "codex", "code assistant",
            "ai assistant", "llm", "language model", "gpt",
            "tabnine", "code generation", "code completion", "ai chat",
            "language model", "artificial intelligence", "machine learning",
            "azure openai", "vertex ai", "bedrock", "sagemaker",
        ]
        
        mcp_plugins = []
        detected_ids = set()
        
        for ext in extensions:
            ext_id = str(ext.get("id", ""))
            
            if ext_id in known_mcp_plugins:
                plugin_info = known_mcp_plugins[ext_id]
                mcp_plugins.append({
                    "id": ext_id,
                    "name": ext.get("name", plugin_info["name"]),
                    "category": plugin_info["category"],
                    "capabilities": plugin_info["capabilities"],
                    "version": ext.get("version"),
                    "path": ext.get("path"),
                    "source": "known_database"
                })
                detected_ids.add(ext_id)
                continue
            
            ext_name = str(ext.get("name", "")).lower()
            ext_id_lower = ext_id.lower()
            ext_desc = str(ext.get("description", "")).lower()
            
            for keyword in mcp_keywords:
                if keyword in ext_name or keyword in ext_id_lower or keyword in ext_desc:
                    mcp_plugins.append({
                        "id": ext_id,
                        "name": ext.get("name"),
                        "category": "ai_mcp",
                        "capabilities": ["mcp"],
                        "version": ext.get("version"),
                        "path": ext.get("path"),
                        "source": "keyword_match",
                        "keyword_matched": keyword,
                    })
                    break
        
        return mcp_plugins
    
    def get_mcp_capabilities(self) -> Dict[str, Any]:
        """IDE'nin MCP yeteneklerini analiz et"""
        extensions = self._detected.extensions
        mcp_plugins = self._detect_mcp_plugins(extensions)
        
        capabilities = {
            "ai_code_completion": False,
            "ai_chat": False,
            "ai_assistant": False,
            "mcp_protocol": False,
            "lsp_server": False,
            "remote_development": False,
            "testing": False,
            "container_dev": False,
            "cloud_integration": False,
        }
        
        for plugin in mcp_plugins:
            caps = plugin.get("capabilities", [])
            for cap in caps:
                if "ai_completion" in caps:
                    capabilities["ai_code_completion"] = True
                if "ai_chat" in caps or "ai" in caps:
                    capabilities["ai_chat"] = True
                if "assistant" in caps:
                    capabilities["ai_assistant"] = True
                if "mcp" in caps:
                    capabilities["mcp_protocol"] = True
                if "lsp" in caps:
                    capabilities["lsp_server"] = True
                if "remote" in caps:
                    capabilities["remote_development"] = True
                if "testing" in caps:
                    capabilities["testing"] = True
                if "docker" in caps or "containers" in caps:
                    capabilities["container_dev"] = True
                if "aws" in caps or "azure" in caps or "cloud" in caps:
                    capabilities["cloud_integration"] = True
        
        return capabilities
    
    def get_ide_info(self) -> Dict[str, Any]:
        """IDE bilgilerini dict olarak döndür"""
        mcp_plugins = self._detect_mcp_plugins(self._detected.extensions)
        capabilities = self.get_mcp_capabilities()
        
        info = {
            "ide_type": self._detected.ide_type.value,
            "name": self._detected.name,
            "version": self._detected.version,
            "workspace_path": str(self._detected.workspace_path) if self._detected.workspace_path else None,
            "config_dir": str(self._detected.config_dir) if self._detected.config_dir else None,
            "extensions": self._detected.extensions,
            "supports_mcp": self._detected.supports_mcp,
            "mcp_config_location": str(self._detected.mcp_config_location) if self._detected.mcp_config_location else None,
            "detection_method": self._detected.detection_method,
            "mcp_plugins": mcp_plugins,
            "mcp_plugins_count": len(mcp_plugins),
            "extensions_count": len(self._detected.extensions),
            "capabilities": capabilities,
            "has_ai_capabilities": any(capabilities.values()),
            "has_mcp_support": capabilities["mcp_protocol"],
        }
        return info

def detect_ide() -> IDEDetection:
    """IDE tespiti için kolay erişim fonksiyonu"""
    return IDEDetector().detected

def is_ide_supported() -> bool:
    """IDE desteği kontrolü"""
    return IDEDetector().is_supported()

def get_mcp_config() -> Dict[str, Any]:
    """MCP konfigürasyonu al"""
    return IDEDetector().get_mcp_config_template()

if __name__ == "__main__":
    import json
    
    detector = IDEDetector()
    info = detector.get_ide_info()
    
    print(json.dumps({
        "success": True,
        "detected": info,
        "supported": detector.is_supported(),
        "config_template": detector.get_mcp_config_template() if detector.is_supported() else None
    }, indent=2))
