"""
Smart Linker utility for NativMix.
Handles dynamic port discovery, clean link management, and robust routing chains.
"""

import subprocess
import re
import logging
import json

logger = logging.getLogger(__name__)

# Maximum seconds to wait for any pw-link / pw-dump / pactl call.
# If PipeWire hangs, the call raises subprocess.TimeoutExpired instead of
# blocking the caller thread indefinitely.
_SUBPROCESS_TIMEOUT = 5

def find_ports(node_pattern: str, direction: str = "output", port_pattern: str | None = None) -> list[str]:
    """
    Find ports for a given node pattern and direction.
    If node_pattern looks like an integer, it's treated as a Pulse Module ID
    and we use pw-dump to find the corresponding PipeWire node.
    """
    target_node_name = node_pattern
    
    # If node_pattern is a Pulse Module ID (integer), resolve it via pw-dump
    if node_pattern.isdigit():
        try:
            dump_res = subprocess.run(["pw-dump"], capture_output=True, text=True, check=True,
                                      timeout=_SUBPROCESS_TIMEOUT)
            nodes = json.loads(dump_res.stdout)
            for n in nodes:
                if n.get("type") == "PipeWire:Interface:Node":
                    props = n.get("info", {}).get("props", {})
                    if str(props.get("pulse.module.id")) == node_pattern:
                        target_node_name = props.get("node.name")
                        logger.debug("SmartLinker: Resolved Pulse ID %s to Node %s", node_pattern, target_node_name)
                        break
        except Exception as e:
            logger.warning("SmartLinker: Failed to resolve Pulse ID %s via pw-dump: %s", node_pattern, e)

    cmd = ["pw-link", "-o" if direction == "output" else "-i"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                timeout=_SUBPROCESS_TIMEOUT)
        all_ports = result.stdout.splitlines()
        
        matched_ports = []
        for port in all_ports:
            if ":" in port:
                node, port_name = port.split(":", 1)
                # Use exact match if we resolved it, else regex
                if (target_node_name == node) or re.search(target_node_name, node):
                    if port_pattern is None or re.search(port_pattern, port_name):
                        matched_ports.append(port)
        
        return matched_ports
    except subprocess.TimeoutExpired:
        logger.warning("pw-link port listing timed out after %ds (node=%s)", _SUBPROCESS_TIMEOUT, node_pattern)
        return []
    except subprocess.CalledProcessError as e:
        logger.warning("Failed to find ports for node=%s, port=%s (%s): %s",
                       node_pattern, port_pattern, direction, e.stderr)
        return []

def clean_links(source_node: str | None = None, target_node: str | None = None) -> None:
    """
    Remove all existing links between specific nodes or globally if requested.
    Uses 'pw-link -d' to ensure the state is clean.
    """
    try:
        # Get all current links
        result = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, check=True,
                                timeout=_SUBPROCESS_TIMEOUT)
        
        # pw-link -l output format: 'SourceNode:SourcePort -> TargetNode:TargetPort'
        for line in result.stdout.splitlines():
            if " -> " not in line:
                continue
                
            src, dst = line.split(" -> ", 1)
            src_node = src.split(":", 1)[0] if ":" in src else src
            dst_node = dst.split(":", 1)[0] if ":" in dst else dst
            
            should_delete = False
            if source_node and target_node:
                if re.search(source_node, src_node) and re.search(target_node, dst_node):
                    should_delete = True
            elif source_node:
                if re.search(source_node, src_node):
                    should_delete = True
            elif target_node:
                if re.search(target_node, dst_node):
                    should_delete = True
                    
            if should_delete:
                logger.debug("SmartLinker: Deleting redundant link: %s -> %s", src, dst)
                try:
                    subprocess.run(["pw-link", "-d", src, dst], capture_output=True,
                                   timeout=_SUBPROCESS_TIMEOUT)
                except subprocess.TimeoutExpired:
                    logger.warning("pw-link -d timed out after %ds (%s -> %s)", _SUBPROCESS_TIMEOUT, src, dst)

    except subprocess.TimeoutExpired:
        logger.warning("pw-link -l timed out after %ds", _SUBPROCESS_TIMEOUT)
    except subprocess.CalledProcessError as e:
        logger.warning("Failed to clean links: %s", e.stderr)

def smart_link(source_pattern: str, target_pattern: str, 
               source_dir: str = "output", target_dir: str = "input",
               source_port_pattern: str | None = None, target_port_pattern: str | None = None) -> bool:
    """
    Link source ports to target ports by index.
    Ensures Independence of naming conventions (FL/FR vs AUX0/1).
    Includes a small retry loop for PipeWire registration latency.
    """
    import time
    
    source_ports = []
    target_ports = []

    # Retry loop: wait up to 2 seconds (10 × 200 ms).
    # 200 ms gives PipeWire enough time to register new loopback ports on CachyOS.
    # The first two failed attempts are logged at DEBUG so normal startup delays
    # don't pollute the log; a WARNING is only emitted when all retries are exhausted.
    _MAX_RETRIES = 10
    for attempt in range(_MAX_RETRIES):
        source_ports = find_ports(source_pattern, direction=source_dir, port_pattern=source_port_pattern)
        target_ports = find_ports(target_pattern, direction=target_dir, port_pattern=target_port_pattern)

        if source_ports and target_ports:
            break

        if attempt < 2:
            if not source_ports:
                logger.debug(
                    "SmartLinker: waiting for source ports, attempt %d/%d (node='%s')",
                    attempt + 1, _MAX_RETRIES, source_pattern,
                )
            if not target_ports:
                logger.debug(
                    "SmartLinker: waiting for target ports, attempt %d/%d (node='%s')",
                    attempt + 1, _MAX_RETRIES, target_pattern,
                )

        time.sleep(0.2)

    if not source_ports:
        logger.warning(
            "SmartLinker: No source ports found for node='%s', port='%s' after %d retries",
            source_pattern, source_port_pattern, _MAX_RETRIES,
        )
        return False
    if not target_ports:
        logger.warning(
            "SmartLinker: No target ports found for node='%s', port='%s' after %d retries",
            target_pattern, target_port_pattern, _MAX_RETRIES,
        )
        return False
    
    # Sort to ensure consistent pairing by index
    source_ports.sort()
    target_ports.sort()
    
    # We pair by index. If counts don't match, we link what we can.
    link_count = min(len(source_ports), len(target_ports))
    success = False
    
    for i in range(link_count):
        src = source_ports[i]
        dst = target_ports[i]
        
        try:
            # pw-link fails silently if already linked, which is fine
            subprocess.run(["pw-link", src, dst], capture_output=True, check=True,
                           timeout=_SUBPROCESS_TIMEOUT)
            logger.debug("SmartLinker: Linked %s -> %s", src, dst)
            success = True
        except subprocess.TimeoutExpired:
            logger.warning("pw-link timed out after %ds (%s -> %s)", _SUBPROCESS_TIMEOUT, src, dst)
        except subprocess.CalledProcessError:
            # Often happens if already linked — not an error
            pass
            
    return success
