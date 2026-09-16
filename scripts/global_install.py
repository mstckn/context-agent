#!/usr/bin/env python3
"""
Global Kurulum ve Otomatik Konfigürasyon Sistemi

Context Agent'ı tüm sistemde tek bir komutla kurar ve yapılandırır.
Özellikler:
- Tüm IDE'ler için otomatik MCP konfigürasyonu
- Sistem çapında kurulum
- Token-based authentication
- Proje bağımsız çalışma
- Otomatik güncelleme mekanizması

Kullanım:
  python global_install.py --install
  python global_install.py --uninstall
  python global_install.py --update
  python global_install.py --status
"""

import os
import sys
import json
import shutil
import subprocess
import platform
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

@dataclass
class InstallationConfig:
    install_path: Path
    version: str
    installed_at: datetime
    ide_configs: List[str]
    python_path: str
    platform: str

class GlobalInstaller:
    VERSION = "1.0.0"
    
    def __init__(self):
        self.home = Path.home()
        self.install_dir = self.home / ".context-agent"
        self.config_file = self.install_dir / "config.json"
        self.ide_configs_dir = self.install_dir / "ide-configs"
        self.log_file = self.install_dir / "install.log"
        
        self._detect_python()
        self._detect_source_dir()
    
    def _detect_python(self):
        """Python yorumlayıcısını bul"""
        self.python_exe = Path(sys.executable)
        
        result = subprocess.run(
            [str(self.python_exe), "--version"],
            capture_output=True,
            text=True
        )
        self.python_version = result.stdout.strip()
    
    def _detect_source_dir(self):
        """Kaynak dizinini bul"""
        current = Path(__file__).resolve().parent
        
        if (current.parent / ".context").exists():
            self.source_dir = current.parent
        elif (current.parent.parent / ".context").exists():
            self.source_dir = current.parent.parent
        else:
            self.source_dir = current
        
        self.launcher_source = self.source_dir / "scripts" / "mcp_launcher.py"
        self.server_source = self.source_dir / "scripts" / "mcp_server.py"
        self.agent_source = self.source_dir / "scripts" / "agent.py"
        self.index_source = self.source_dir / "scripts" / "index.py"
    
    def _log(self, message: str, level: str = "INFO"):
        """Log mesajı yaz"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] [{level}] {message}\n"
        
        self.install_dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(log_line)
        
        print(log_line.strip())
    
    def _load_config(self) -> Optional[InstallationConfig]:
        """Konfigürasyon dosyasını oku"""
        if not self.config_file.exists():
            return None
        
        try:
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
            return InstallationConfig(
                install_path=Path(data["install_path"]),
                version=data["version"],
                installed_at=datetime.fromisoformat(data["installed_at"]),
                ide_configs=data.get("ide_configs", []),
                python_path=data["python_path"],
                platform=data["platform"]
            )
        except Exception:
            return None
    
    def _save_config(self, config: InstallationConfig):
        """Konfigürasyon dosyasını kaydet"""
        self.install_dir.mkdir(parents=True, exist_ok=True)
        
        data = {
            "install_path": str(config.install_path),
            "version": config.version,
            "installed_at": config.installed_at.isoformat(),
            "ide_configs": config.ide_configs,
            "python_path": config.python_path,
            "platform": config.platform
        }
        
        self.config_file.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8"
        )
    
    def _create_launcher(self) -> bool:
        """Global launcher oluştur"""
        launcher_content = f'''#!/usr/bin/env python3
"""
Context Agent Global Launcher

Otomatik olarak oluşturulmuştur.
Tüm projeler için merkezi giriş noktası.
"""

import sys
import os
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CONTEXT_AGENT_PATH = Path(r"{self.install_dir}")
SOURCE_PATH = Path(r"{self.source_dir}")

def find_project_root():
    """Proje kökünü bul"""
    env_root = os.environ.get("CONTEXT_AGENT_PROJECT_ROOT") or os.environ.get("MCP_PROJECT_ROOT")
    if env_root:
        return Path(env_root).resolve()

    if "--project-root" in sys.argv:
        index = sys.argv.index("--project-root")
        if index + 1 < len(sys.argv):
            value = sys.argv[index + 1]
            # Unexpanded IDE placeholders are not filesystem paths. Fall
            # through to cwd discovery when the host leaves one untouched.
            if value and "${{" not in value:
                candidate = Path(value).expanduser().resolve()
                if candidate.exists() and candidate.is_dir():
                    return candidate
    
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / ".context").exists():
            return parent
    return current

def main():
    root = find_project_root()
    sys.path.insert(0, str(SOURCE_PATH / "scripts"))
    
    os.environ["CONTEXT_AGENT_PROJECT_ROOT"] = str(root)
    os.environ["CONTEXT_AGENT_IDE"] = os.environ.get("IDE", "unknown")
    
    launcher_path = SOURCE_PATH / "scripts" / "mcp_launcher.py"
    
    if not launcher_path.exists():
        print("ERROR: mcp_launcher.py not found in source directory")
        sys.exit(1)
    
    os.chdir(str(root))
    os.execv(sys.executable, [sys.executable, str(launcher_path), *sys.argv[1:]])

if __name__ == "__main__":
    main()
'''
        
        launcher_path = self.install_dir / "launcher.py"
        launcher_path.write_text(launcher_content, encoding="utf-8")
        
        return launcher_path.exists()
    
    def _create_ide_configs(self) -> Dict[str, bool]:
        """IDE konfigürasyonlarını oluştur"""
        configs = {}
        
        self.ide_configs_dir.mkdir(parents=True, exist_ok=True)
        
        configs["vscode"] = self._create_vscode_config()
        configs["cursor"] = self._create_cursor_config()
        configs["jetbrains"] = self._create_jetbrains_config()
        configs["neovim"] = self._create_neovim_config()
        configs["windsurf"] = self._create_windsurf_config()
        configs["trae"] = self._create_trae_config()
        
        return configs
    
    def _create_vscode_config(self) -> bool:
        """VSCode MCP konfigürasyonu oluştur"""
        config = {
            "mcpServers": {
                "context-agent": {
                    "command": str(self.python_exe),
                    "args": [str(self.install_dir / "launcher.py"),
                             "--project-root", "${workspaceFolder}"],
                    "env": {
                        "CONTEXT_AGENT_IDE": "vscode",
                        "CONTEXT_AGENT_PROJECT_ROOT": "${workspaceFolder}"
                    }
                }
            }
        }
        
        config_path = self.ide_configs_dir / "vscode-mcp.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        
        return config_path.exists()
    
    def _create_cursor_config(self) -> bool:
        """Cursor IDE MCP konfigürasyonu oluştur"""
        config = {
            "mcpServers": {
                "context-agent": {
                    "command": str(self.python_exe),
                    "args": [str(self.install_dir / "launcher.py"),
                             "--project-root", "${workspaceFolder}"],
                    "env": {
                        "CONTEXT_AGENT_IDE": "cursor"
                    }
                }
            }
        }
        
        config_path = self.ide_configs_dir / "cursor-mcp.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        
        return config_path.exists()
    
    def _create_jetbrains_config(self) -> bool:
        """JetBrains MCP konfigürasyonu oluştur"""
        config = {
            "mcpServers": {
                "context-agent": {
                    "command": str(self.python_exe),
                    "args": [str(self.install_dir / "launcher.py"),
                             "--project-root", "${workspaceFolder}"],
                    "env": {
                        "CONTEXT_AGENT_IDE": "jetbrains"
                    }
                }
            }
        }
        
        config_path = self.ide_configs_dir / "jetbrains-mcp.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        
        return config_path.exists()
    
    def _create_neovim_config(self) -> bool:
        """Neovim MCP konfigürasyonu oluştur"""
        config = f'''-- Context Agent MCP Configuration for Neovim
-- Generated by global_install.py

local mcp_config = {{
    {{
        name = "context-agent",
        command = "{self.python_exe}",
        args = {{"{self.install_dir / "launcher.py"}", "--project-root", vim.fn.getcwd()}},
        env = {{
            CONTEXT_AGENT_IDE = "neovim"
        }}
    }}
}}

-- Initialize MCP with nvim-mcp or fidget
-- Example with nvim-mcp:
-- require("mcp").setup(mcp_config)
'''
        
        config_path = self.ide_configs_dir / "neovim-mcp.lua"
        config_path.write_text(config, encoding="utf-8")
        
        return config_path.exists()
    
    def _create_windsurf_config(self) -> bool:
        """Windsurf MCP konfigürasyonu oluştur"""
        config = {
            "mcpServers": {
                "context-agent": {
                    "command": str(self.python_exe),
                    "args": [str(self.install_dir / "launcher.py"),
                             "--project-root", "${workspaceFolder}"],
                    "env": {
                        "CONTEXT_AGENT_IDE": "windsurf"
                    }
                }
            }
        }
        
        config_path = self.ide_configs_dir / "windsurf-mcp.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        
        return config_path.exists()
    
    def _create_trae_config(self) -> bool:
        """Trae IDE MCP konfigürasyonu oluştur"""
        config = {
            "mcpServers": {
                "context-agent": {
                    "command": str(self.python_exe),
                    "args": [str(self.install_dir / "launcher.py"),
                             "--project-root", "${workspaceFolder}"],
                    "env": {
                        "CONTEXT_AGENT_IDE": "trae"
                    }
                }
            }
        }
        
        config_path = self.ide_configs_dir / "trae-mcp.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        
        return config_path.exists()
    
    def _install_to_vscode(self) -> bool:
        """VSCode'a otomatik kur"""
        try:
            vscode_settings = self.home / ".config" / "Code" / "User" / "settings.json"
            vscode_settings.parent.mkdir(parents=True, exist_ok=True)
            
            config = {
                "mcpServers": {
                    "context-agent": {
                        "command": str(self.python_exe),
                        "args": [str(self.install_dir / "launcher.py"),
                                 "--project-root", "${workspaceFolder}"],
                        "env": {
                            "CONTEXT_AGENT_IDE": "vscode"
                        }
                    }
                }
            }
            
            if vscode_settings.exists():
                existing = json.loads(vscode_settings.read_text(encoding="utf-8"))
                if "mcpServers" in existing:
                    existing["mcpServers"]["context-agent"] = config["mcpServers"]["context-agent"]
                else:
                    existing["mcpServers"] = config["mcpServers"]
                config = existing
            
            vscode_settings.write_text(json.dumps(config, indent=4), encoding="utf-8")
            
            self._log("VSCode MCP configuration installed")
            return True
        
        except Exception as e:
            self._log(f"Failed to install to VSCode: {e}", "ERROR")
            return False
    
    def _install_to_cursor(self) -> bool:
        """Cursor IDE'ye otomatik kur"""
        try:
            cursor_settings = self.home / ".cursor" / "settings.json"
            cursor_settings.parent.mkdir(parents=True, exist_ok=True)
            
            config = {
                "mcpServers": {
                    "context-agent": {
                        "command": str(self.python_exe),
                        "args": [str(self.install_dir / "launcher.py"),
                                 "--project-root", "${workspaceFolder}"],
                        "env": {
                            "CONTEXT_AGENT_IDE": "cursor"
                        }
                    }
                }
            }
            
            if cursor_settings.exists():
                existing = json.loads(cursor_settings.read_text(encoding="utf-8"))
                if "mcpServers" in existing:
                    existing["mcpServers"]["context-agent"] = config["mcpServers"]["context-agent"]
                else:
                    existing["mcpServers"] = config["mcpServers"]
                config = existing
            
            cursor_settings.write_text(json.dumps(config, indent=4), encoding="utf-8")
            
            self._log("Cursor MCP configuration installed")
            return True
        
        except Exception as e:
            self._log(f"Failed to install to Cursor: {e}", "ERROR")
            return False
    
    def install(self, install_ide_configs: bool = True) -> bool:
        """Global kurulumu gerçekleştir"""
        self._log(f"Starting Context Agent global installation (v{self.VERSION})")
        self._log(f"Install directory: {self.install_dir}")
        self._log(f"Python: {self.python_version}")
        self._log(f"Platform: {platform.system()}")
        
        self.install_dir.mkdir(parents=True, exist_ok=True)
        
        self._log("Creating launcher script...")
        if not self._create_launcher():
            self._log("Failed to create launcher", "ERROR")
            return False
        
        self._log("Creating IDE configuration templates...")
        configs = self._create_ide_configs()
        for ide, success in configs.items():
            status = "OK" if success else "FAILED"
            self._log(f"  {ide}: {status}")
        
        if install_ide_configs:
            self._log("Installing MCP configurations to IDEs...")
            self._install_to_vscode()
            self._install_to_cursor()
        
        config = InstallationConfig(
            install_path=self.install_dir,
            version=self.VERSION,
            installed_at=datetime.now(),
            ide_configs=list(configs.keys()),
            python_path=str(self.python_exe),
            platform=platform.system()
        )
        self._save_config(config)
        
        self._log("Context Agent installed successfully!")
        self._log(f"\nNext steps:")
        self._log(f"  1. Restart your IDE")
        self._log(f"  2. Context Agent will automatically activate for any project")
        self._log(f"  3. Check status with: python {self.install_dir / 'launcher.py'} --status")
        
        return True
    
    def uninstall(self) -> bool:
        """Kurulumu kaldır"""
        self._log("Uninstalling Context Agent...")
        
        vscode_settings = self.home / ".config" / "Code" / "User" / "settings.json"
        if vscode_settings.exists():
            try:
                config = json.loads(vscode_settings.read_text(encoding="utf-8"))
                if "mcpServers" in config and "context-agent" in config["mcpServers"]:
                    del config["mcpServers"]["context-agent"]
                    vscode_settings.write_text(json.dumps(config, indent=4), encoding="utf-8")
                    self._log("Removed VSCode MCP configuration")
            except Exception as e:
                self._log(f"Failed to remove VSCode config: {e}", "ERROR")
        
        cursor_settings = self.home / ".cursor" / "settings.json"
        if cursor_settings.exists():
            try:
                config = json.loads(cursor_settings.read_text(encoding="utf-8"))
                if "mcpServers" in config and "context-agent" in config["mcpServers"]:
                    del config["mcpServers"]["context-agent"]
                    cursor_settings.write_text(json.dumps(config, indent=4), encoding="utf-8")
                    self._log("Removed Cursor MCP configuration")
            except Exception as e:
                self._log(f"Failed to remove Cursor config: {e}", "ERROR")
        
        if self.install_dir.exists():
            shutil.rmtree(self.install_dir)
            self._log("Removed installation directory")
        
        self._log("Context Agent uninstalled successfully")
        return True
    
    def update(self) -> bool:
        """Güncelleme kontrolü yap"""
        config = self._load_config()
        if not config:
            self._log("No installation found", "ERROR")
            return False
        
        current_version = config.version
        self._log(f"Current version: {current_version}")
        self._log(f"Latest version: {self.VERSION}")
        
        if current_version != self.VERSION:
            self._log("New version available. Run install again to update.")
            return self.install()
        
        self._log("Already up to date")
        return True
    
    def status(self) -> Dict[str, Any]:
        """Kurulum durumunu göster"""
        config = self._load_config()
        
        status = {
            "installed": config is not None,
            "version": config.version if config else None,
            "install_path": str(config.install_path) if config else None,
            "installed_at": config.installed_at.isoformat() if config else None,
            "platform": config.platform if config else platform.system(),
            "python": self.python_version,
            "ide_configs_exist": (self.ide_configs_dir / "vscode-mcp.json").exists(),
            "launcher_exists": (self.install_dir / "launcher.py").exists(),
        }
        
        return status

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Context Agent Global Installer")
    parser.add_argument("--install", action="store_true", help="Install globally")
    parser.add_argument("--uninstall", action="store_true", help="Uninstall")
    parser.add_argument("--update", action="store_true", help="Update to latest")
    parser.add_argument("--status", action="store_true", help="Show installation status")
    parser.add_argument("--skip-ide-configs", action="store_true", help="Skip IDE configuration")
    
    args = parser.parse_args()
    
    installer = GlobalInstaller()
    
    if args.install:
        success = installer.install(not args.skip_ide_configs)
        sys.exit(0 if success else 1)
    elif args.uninstall:
        success = installer.uninstall()
        sys.exit(0 if success else 1)
    elif args.update:
        success = installer.update()
        sys.exit(0 if success else 1)
    elif args.status:
        status = installer.status()
        print(json.dumps(status, indent=2))
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
