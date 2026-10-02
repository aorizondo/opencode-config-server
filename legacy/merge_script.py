#!/usr/bin/env python3
"""
Merge script for OpenCode Enterprise Config Server.
Performs intelligent merge of organization config into user config.
User configuration always wins (is preserved).
"""
import json
import yaml
import shutil
import sys
from pathlib import Path


def deep_merge_json(target_file: Path, src_file: Path):
    """Deep merge JSON: user (target) wins over org (src)."""
    if not src_file.exists():
        return
    
    with open(src_file, 'r') as f:
        src_data = json.load(f)
    
    if target_file.exists():
        with open(target_file, 'r') as f:
            target_data = json.load(f)
    else:
        target_data = {}
    
    def merge_dict(tgt, src):
        for key, value in src.items():
            if key in tgt:
                if isinstance(tgt[key], dict) and isinstance(value, dict):
                    merge_dict(tgt[key], value)
                # User wins: keep target value, don't overwrite
            else:
                tgt[key] = value
    
    merge_dict(target_data, src_data)
    
    with open(target_file, 'w') as f:
        json.dump(target_data, f, indent=2)


def deep_merge_yaml(target_file: Path, src_file: Path):
    """Deep merge YAML: user (target) wins over org (src)."""
    if not src_file.exists():
        return
    
    with open(src_file, 'r') as f:
        src_data = yaml.safe_load(f) or {}
    
    if target_file.exists():
        with open(target_file, 'r') as f:
            target_data = yaml.safe_load(f) or {}
    else:
        target_data = {}
    
    def merge_dict(tgt, src):
        for key, value in src.items():
            if key in tgt:
                if isinstance(tgt[key], dict) and isinstance(value, dict):
                    merge_dict(tgt[key], value)
                # User wins: keep target value
            else:
                tgt[key] = value
    
    merge_dict(target_data, src_data)
    
    with open(target_file, 'w') as f:
        yaml.dump(target_data, f, default_flow_style=False, sort_keys=False)


def merge_rules(target_rules: Path, src_rules: Path):
    """Merge rules: only copy files that don't exist in target."""
    if not src_rules.exists():
        return
    target_rules.mkdir(parents=True, exist_ok=True)
    for rule_file in src_rules.glob("*.md"):
        target_file = target_rules / rule_file.name
        if not target_file.exists():
            shutil.copy2(rule_file, target_file)


def merge_env(target_env: Path, src_env_vars: dict):
    """Merge environment variables: user wins."""
    existing = {}
    if target_env.exists():
        with open(target_env, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('export '):
                    parts = line[7:].split('=', 1)
                    if len(parts) == 2:
                        existing[parts[0]] = parts[1]
    
    # Org vars only added if not present
    for key, value in src_env_vars.items():
        if key not in existing:
            existing[key] = value
    
    with open(target_env, 'w') as f:
        f.write("# CONFIGURACIÓN DE ENTORNO CORPORATIVO COMPARTIDO (mergeado)\n")
        for key, value in existing.items():
            f.write(f"export {key}={value}\n")


def main():
    if len(sys.argv) != 3:
        print("Usage: merge_script.py <target_dir> <src_dir>", file=sys.stderr)
        sys.exit(1)
    
    target = Path(sys.argv[1])
    src = Path(sys.argv[2])
    
    if not target.exists():
        print(f"Target directory does not exist: {target}", file=sys.stderr)
        sys.exit(1)
    
    if not src.exists():
        print(f"Source directory does not exist: {src}", file=sys.stderr)
        sys.exit(1)
    
    # Merge config.json
    deep_merge_json(target / "config.json", src / "config.json")
    
    # Merge rules (only add new)
    merge_rules(target / "rules", src / "rules")
    
    # Merge plugins (only add new)
    merge_rules(target / "plugins", src / "plugins")
    
    # Merge OMO config.yaml
    omo_target = Path.home() / ".omo"
    omo_target.mkdir(parents=True, exist_ok=True)
    deep_merge_yaml(omo_target / "config.yaml", src / "config.yaml")
    
    # Merge environment variables
    org_env = {
        "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY:-}",
        "OPENCODE_JIRA_TOKEN": "${OPENCODE_JIRA_TOKEN:-}",
        "OMO_ENGINE_ROUTE": "https://internal.corp",
        "OPENCODE_RULES_STRICT_MODE": "true"
    }
    merge_env(Path.home() / ".opencode.env", org_env)
    
    print("Merge completed successfully")


if __name__ == "__main__":
    main()