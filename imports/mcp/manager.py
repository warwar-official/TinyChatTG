"""Simple MCP manager to launch stdio-based MCP processes and send/receive JSON lines.

This manager supports connecting to multiple servers via persistent threads,
synchronizing configuration permissions and visibility, and exposing a unified toolset.
"""
import json
import os
import subprocess
import threading
import queue
import time
import yaml
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class MCPServerConnection:
    def __init__(self, name: str, cfg: Dict[str, Any]):
        self.name = name
        self.cfg = cfg
        self.command = cfg.get("command")
        self.args = cfg.get("args", [])
        self.env = cfg.get("env", {})
        self.proc = None
        self.queue = queue.Queue()
        self._stop_event = threading.Event()
        self.thread = None
        self._msg_id = 0
        self._id_lock = threading.Lock()

    def _next_id(self):
        with self._id_lock:
            self._msg_id += 1
            return self._msg_id

    def start(self):
        if not self.command:
            raise RuntimeError(f"No command configured for {self.name}")
        cmd = [self.command] + list(self.args)
        env = os.environ.copy()
        env.update(self.env or {})
        
        try:
            # Use text mode and line-buffering where possible
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
            )
        except Exception as e:
            raise RuntimeError(f"Failed to start {self.name}: {e}")

        self._stop_event.clear()
        self.thread = threading.Thread(target=self._reader_thread, name=f"mcp-reader-{self.name}", daemon=True)
        self.thread.start()

        # MCP Handshake
        init_req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "TinyChatTG", "version": "1.0.0"}
            }
        }
        init_res = self.send_sync(init_req, timeout=10.0)
        
        # Send initialized notification
        self._send_async({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {}
        })

    def _send_async(self, obj: Dict[str, Any]):
        if not self.proc or self.proc.poll() is not None:
            return
        payload = json.dumps(obj, ensure_ascii=False) + "\n"
        try:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()
        except Exception:
            pass

    def _reader_thread(self):
        out_buf = ''
        while not self._stop_event.is_set() and self.proc and self.proc.poll() is None:
            try:
                line = self.proc.stdout.readline()
                if not line:
                    time.sleep(0.01)
                    continue
                out_buf += line
                try:
                    obj = json.loads(out_buf)
                    if "id" in obj:
                        self.queue.put(obj)
                    out_buf = ''
                except ValueError:
                    # Keep reading until valid JSON
                    continue
            except Exception:
                break

    def stop(self):
        self._stop_event.set()
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
            self.proc = None
        if self.thread:
            self.thread.join(timeout=1)

    def send_sync(self, obj: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError(f"MCP {self.name} is not running")
        
        # Clear queue to ignore any unexpected previous messages
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
                
        payload = json.dumps(obj, ensure_ascii=False) + "\n"
        try:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()
        except Exception as e:
            raise RuntimeError(f"Failed to write to {self.name} stdin: {e}")
            
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return {"error": "timeout"}

    def list_tools(self):
        """Ask the MCP process for available tools."""
        try:
            req = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/list",
                "params": {}
            }
            res = self.send_sync(req)
            if isinstance(res, dict):
                result = res.get('result', {})
                if 'tools' in result:
                    return result['tools']
            return []
        except Exception:
            return []


class MCPManager:
    def __init__(self, mcp_cfg: Dict[str, Any] = None, data_dir: str = None):
        self.cfg = mcp_cfg or {}
        if data_dir:
            self.mcp_dir = Path(data_dir)
        else:
            from imports.config import PROJECT_ROOT
            self.mcp_dir = PROJECT_ROOT / "data" / "mcp"
        
        self.connections: Dict[str, MCPServerConnection] = {}
        self.reports: List[str] = []
        self.unified_tools: Dict[str, Any] = {}
        self.app_tools: Dict[str, Any] = {}

    def _add_report(self, msg: str):
        self.reports.append(msg)
        logger.info(f"[MCP Report] {msg}")

    def start(self):
        self.mcp_dir.mkdir(parents=True, exist_ok=True)
        self.reports.clear()
        
        # Load app_tools.yaml
        app_tools_path = self.mcp_dir / "app_tools.yaml"
        if app_tools_path.exists():
            try:
                with open(app_tools_path, 'r', encoding='utf-8') as f:
                    app_cfg = yaml.safe_load(f) or {}
                self.app_tools = app_cfg.get("tools", {})
            except Exception as e:
                self._add_report(f"Failed to load app_tools.yaml: {e}")
        else:
            # Removed legacy tools.yaml migration as requested
            pass

        # Connect to servers
        for name, server_cfg in self.cfg.items():
            if not server_cfg.get('enabled', True):
                continue
            
            conn = MCPServerConnection(name, server_cfg)
            try:
                conn.start()
                self.connections[name] = conn
                self._add_report(f"Connected to MCP server: {name}")
            except Exception as e:
                self._add_report(f"Failed to connect to MCP server {name}: {e}")
                
        # Sync configs
        self.unified_tools = {}
        # Add app tools first
        for tname, tcfg in self.app_tools.items():
            tcfg['_provider'] = 'app'
            self.unified_tools[tname] = tcfg

        for name, conn in list(self.connections.items()):
            config_path = self.mcp_dir / f"{name}.yaml"
            server_tools_list = conn.list_tools()
            if not isinstance(server_tools_list, list):
                server_tools_list = []
                
            server_tools_map = {t.get("name") or t.get("tool") or t.get("id"): t for t in server_tools_list if isinstance(t, dict)}
            
            file_cfg = None
            if config_path.exists():
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        file_cfg = yaml.safe_load(f)
                    if not isinstance(file_cfg, dict):
                        raise ValueError("YAML root is not a dictionary")
                except Exception as e:
                    self._add_report(f"Broken config for server {name}: {e}. Disabling server.")
                    conn.stop()
                    del self.connections[name]
                    continue
                    
            if file_cfg is None:
                # 2.1.3.5 Create new
                file_cfg = {"tools": {}}
                for tname in server_tools_map.keys():
                    file_cfg["tools"][tname] = {"visible": True, "permissions": "ask"}
                self._add_report(f"Created new config for server {name} with {len(server_tools_map)} tools.")
                self._save_yaml(config_path, file_cfg)
            else:
                # 2.1.3.3 Check outdated
                file_tools = file_cfg.get("tools", {})
                changed = False
                # Server tools not in config
                for tname in server_tools_map.keys():
                    if tname not in file_tools:
                        file_tools[tname] = {"visible": True, "permissions": "ask"}
                        changed = True
                        self._add_report(f"New tool '{tname}' found on server {name}. Added to config.")
                
                # Config tools not on server
                for tname in list(file_tools.keys()):
                    if tname not in server_tools_map:
                        if file_tools[tname].get("visible") != False:
                            file_tools[tname]["visible"] = False
                            changed = True
                            self._add_report(f"Tool '{tname}' missing from server {name}. Hidden from model.")
                
                if changed:
                    self._save_yaml(config_path, file_cfg)

            # 2.1.3.4 Load tool schemas from server, permissions from config
            file_tools = file_cfg.get("tools", {})
            for tname, server_schema in server_tools_map.items():
                tcfg = file_tools.get(tname, {})
                
                schema = server_schema.get('schema') or server_schema.get('inputSchema') or server_schema.get('args') or {}
                
                unified_tcfg = {
                    "description": server_schema.get('description', ''),
                    "schema": schema,
                    "visible": tcfg.get("visible", True),
                    "permissions": tcfg.get("permissions", "ask"),
                    "_provider": name
                }
                self.unified_tools[tname] = unified_tcfg
                
    def _save_yaml(self, path, data):
        with open(path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(data, f, default_flow_style=False)

    def stop(self):
        for conn in self.connections.values():
            conn.stop()
        self.connections.clear()

    def get_all_tools(self) -> Dict[str, Any]:
        return {"tools": self.unified_tools}
        
    def send_tool_call(self, server_name: str, tool_name: str, args: dict, user_id: int):
        conn = self.connections.get(server_name)
        if not conn:
            return {"error": f"Server {server_name} not available"}
        
        req = {
            "jsonrpc": "2.0",
            "id": conn._next_id(),
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": args
            }
        }
        res = conn.send_sync(req, timeout=300)
        
        if isinstance(res, dict):
            if "error" in res:
                return {"error": res["error"].get("message", str(res["error"]))}
            
            result = res.get("result", {})
            content = result.get("content", [])
            if content and len(content) > 0:
                return {"result": content[0].get("text", str(content))}
            return {"result": "Success"}
            
        return res
