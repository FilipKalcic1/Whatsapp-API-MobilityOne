# swagger_architect_final.py
# -----------------------------------------------------------------------------
# THE ARCHITECT EDITION: OPENAPI PARSER & ORGANIZER (FINAL STABLE)
# -----------------------------------------------------------------------------
# AUTHOR: Gemini (Senior Python Eng. Persona)
# STATUS: PRODUCTION READY (Fixed Pandas KeyError issue)
# -----------------------------------------------------------------------------

import os
import glob
import json
import re
import pandas as pd
from typing import Dict, Any, List, Optional, Set
from dataclasses import dataclass, asdict

# --- DEPENDENCY CHECK ---
try:
    import yaml
except ImportError:
    yaml = None

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None

# --- CONFIGURATION ---
INPUT_DIR = "./temporary"
OUTPUT_FILE = "./Master_API_Architecture.xlsx"
ACCEPTED_METHODS = {"get", "post", "put", "delete", "patch"}

# --- DATA STRUCTURES ---

@dataclass
class ExtractionContext:
    """Metadata context to track where we are in the JSON tree."""
    service: str
    file: str
    location_type: str  # 'Endpoint' or 'DTO'
    location_name: str
    method: str = ""

    @property
    def label(self) -> str:
        if self.method:
            return f"{self.location_type}: {self.method} {self.location_name}"
        return f"{self.location_type}: {self.location_name}"

@dataclass
class AttributeRow:
    """The atomic unit of our data dictionary."""
    entity_group: str       # KEY FIELD: The normalized name (e.g. "User" from "body.User")
    raw_attribute: str      # The actual path (e.g. "body.User")
    service: str
    context: str
    data_type: str
    required: bool
    nullable: bool
    description: str
    example: str
    enum_values: str
    ref_pointer: str

# --- TEXT ENGINEERING ---

def normalize_entity_name(raw_name: str) -> str:
    """
    Transforms 'response[].Vehicle.Id' -> 'Vehicle.Id'
    Ensures related fields stay together in sorting.
    """
    clean = raw_name
    clean = clean.replace("[]", "")
    
    # Remove structural prefixes
    prefixes = ["body.", "response.", "header.", "query.", "path."]
    for p in prefixes:
        if clean.startswith(p):
            clean = clean.replace(p, "")
            break 
            
    return clean

def clean_html(text: Any) -> str:
    if not text: return ""
    text = str(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    return " ".join(text.split())

def safe_serialize(val: Any) -> str:
    if val is None or val == "": return ""
    if isinstance(val, (dict, list)):
        try:
            return json.dumps(val, ensure_ascii=False)
        except:
            return str(val)
    return str(val)

def get_type_label(schema: Dict) -> str:
    if "$ref" in schema:
        return f"Ref -> {schema['$ref'].split('/')[-1]}"
    
    t = schema.get("type")
    fmt = schema.get("format")
    
    if t == "array":
        items = schema.get("items", {})
        return f"List[{get_type_label(items)}]"
    
    if not t and "properties" in schema:
        return "Object"
    
    if t and fmt:
        return f"{t} ({fmt})"
    
    return str(t) if t else "Unknown"

# --- RECURSIVE EXTRACTOR ENGINE ---

class SchemaExtractor:
    def __init__(self, spec: Dict, filename: str):
        self.spec = spec
        self.filename = filename
        self.resolver_cache = {}

    def resolve_ref(self, ref: str) -> Optional[Dict]:
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        if ref in self.resolver_cache:
            return self.resolver_cache[ref]
        
        try:
            parts = ref.lstrip("#/").split("/")
            node = self.spec
            for p in parts:
                p = p.replace("~1", "/").replace("~0", "~")
                node = node[p]
            self.resolver_cache[ref] = node
            return node
        except:
            return None

    def deep_merge(self, target: Dict, source: Dict) -> Dict:
        for k, v in source.items():
            if isinstance(v, dict) and k in target and isinstance(target[k], dict):
                self.deep_merge(target[k], v)
            else:
                target[k] = v
        return target

    def extract(self, schema: Dict, rows: List[AttributeRow], ctx: ExtractionContext, 
                path_prefix: str = "", visited_refs: Set[str] = None):
        
        if visited_refs is None: visited_refs = set()
        if not isinstance(schema, dict): return

        # 1. Handle Refs
        if "$ref" in schema:
            ref = schema["$ref"]
            if ref in visited_refs: return
            resolved = self.resolve_ref(ref)
            if resolved:
                new_visited = visited_refs.copy()
                new_visited.add(ref)
                self.extract(resolved, rows, ctx, path_prefix, new_visited)
            return

        # 2. Handle Composition (allOf)
        if "allOf" in schema:
            composite = {}
            for part in schema["allOf"]:
                if "$ref" in part:
                    r = self.resolve_ref(part["$ref"])
                    if r: self.deep_merge(composite, r)
                else:
                    self.deep_merge(composite, part)
            for k, v in schema.items():
                if k != "allOf": composite[k] = v
            self.extract(composite, rows, ctx, path_prefix, visited_refs)
            return

        # 3. Extract Properties
        properties = schema.get("properties", {})
        required_set = set(schema.get("required", []) or [])

        if properties:
            for prop_name, prop_data in properties.items():
                full_path = f"{path_prefix}.{prop_name}" if path_prefix else prop_name
                
                desc = prop_data.get("description", "")
                if not desc and "$ref" in prop_data:
                    r = self.resolve_ref(prop_data["$ref"])
                    if r: desc = r.get("description", "")

                row = AttributeRow(
                    entity_group=normalize_entity_name(full_path),
                    raw_attribute=full_path,
                    service=ctx.service,
                    context=ctx.label,
                    data_type=get_type_label(prop_data),
                    required=(prop_name in required_set),
                    nullable=prop_data.get("nullable", False),
                    description=clean_html(desc),
                    example=safe_serialize(prop_data.get("example") or prop_data.get("examples")),
                    enum_values=safe_serialize(prop_data.get("enum")),
                    ref_pointer=prop_data.get("$ref", "")
                )
                rows.append(row)

                if prop_data.get("type") == "object" or "properties" in prop_data:
                    self.extract(prop_data, rows, ctx, full_path, visited_refs.copy())
                elif prop_data.get("type") == "array":
                    items = prop_data.get("items", {})
                    self.extract(items, rows, ctx, f"{full_path}[]", visited_refs.copy())

        elif schema.get("type") == "array":
            self.extract(schema.get("items", {}), rows, ctx, f"{path_prefix}[]", visited_refs)


# --- PROCESSING ORCHESTRATOR ---

def process_files(files: List[str]) -> List[AttributeRow]:
    all_rows = []
    
    for filepath in files:
        filename = os.path.basename(filepath)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                raw = f.read()
            if filepath.endswith(('.yaml', '.yml')):
                if not yaml: continue
                spec = yaml.safe_load(raw)
            else:
                spec = json.loads(raw)
        except Exception as e:
            print(f"❌ Failed to parse {filename}: {e}")
            continue

        if not isinstance(spec, dict): continue

        service_name = spec.get("info", {}).get("title", filename).upper()
        engine = SchemaExtractor(spec, filename)

        defs = spec.get("components", {}).get("schemas", {}) or spec.get("definitions", {})
        for name, schema in defs.items():
            ctx = ExtractionContext(service_name, filename, "DTO", name)
            engine.extract(schema, all_rows, ctx)

        paths = spec.get("paths", {})
        for path, methods in paths.items():
            if not isinstance(methods, dict): continue
            for verb, op in methods.items():
                if verb.lower() not in ACCEPTED_METHODS or not isinstance(op, dict): continue
                
                if "requestBody" in op:
                    content = op["requestBody"].get("content", {})
                    schema = content.get("application/json", {}).get("schema")
                    if schema:
                        ctx = ExtractionContext(service_name, filename, "Endpoint", path, verb.upper())
                        engine.extract(schema, all_rows, ctx, path_prefix="body")

                for code, resp in op.get("responses", {}).items():
                    schema = None
                    if "content" in resp and "application/json" in resp["content"]:
                        schema = resp["content"]["application/json"].get("schema")
                    elif "schema" in resp:
                        schema = resp["schema"]
                    if schema:
                        ctx = ExtractionContext(service_name, filename, "Endpoint", path, f"{verb.upper()} (Resp {code})")
                        engine.extract(schema, all_rows, ctx, path_prefix="response")
                        
    return all_rows

# --- EXCEL ANALYST ---

def generate_report(rows: List[AttributeRow]):
    if not rows:
        print("❌ No data extracted.")
        return

    print("⚙️  Aggregating and Organizing Data...")
    df = pd.DataFrame([asdict(r) for r in rows])

    # AGGREGATION LOGIC
    # NOTE: We do NOT access 'raw_attribute' inside this function anymore
    # because it is excluded by include_groups=False
    def consolidate(x):
        descs = [d for d in x['description'] if d]
        best_desc = sorted(descs, key=len, reverse=True)[0] if descs else ""
        
        ctxs = sorted(list(set(x['context'])))
        ctx_str = "; ".join(ctxs[:4])
        if len(ctxs) > 4: ctx_str += f" ... (+{len(ctxs)-4})"

        services = ", ".join(sorted(list(set(x['service']))))

        return pd.Series({
            "Entity Group": x['entity_group'].iloc[0],
            # Attribute will be restored from index later
            "Consolidated Description": best_desc,
            "USER DESCRIPTION": "",
            "Data Type": x['data_type'].iloc[0],
            "Service": services,
            "Contexts": ctx_str,
            "Required": "Yes" if any(x['required']) else "No",
            "Nullable": "Yes" if any(x['nullable']) else "No",
            "Example": x['example'].iloc[0],
            "Enum": x['enum_values'].iloc[0],
            "Ref": x['ref_pointer'].iloc[0]
        })

    # Grouping
    # include_groups=False hides the grouping key from the applied function (fixes FutureWarning)
    grouped_df = df.groupby('raw_attribute').apply(consolidate, include_groups=False)
    
    # Restore 'raw_attribute' from the index and rename it
    final_df = grouped_df.reset_index()
    final_df.rename(columns={'raw_attribute': 'Attribute'}, inplace=True)

    # SORTING LOGIC
    print("✨ Applying Logic Sort (Entity Grouping)...")
    final_df = final_df.sort_values(by=['Entity Group', 'Attribute'])

    # COLUMN ORDER
    cols = ["Entity Group", "Attribute", "Consolidated Description", "USER DESCRIPTION", "Data Type", "Required", "Nullable", "Enum", "Example", "Service", "Contexts", "Ref"]
    final_df = final_df[cols]

    print(f"💾 Writing {len(final_df)} organized definitions to {OUTPUT_FILE}...")
    
    if xlsxwriter:
        with pd.ExcelWriter(OUTPUT_FILE, engine='xlsxwriter') as writer:
            final_df.to_excel(writer, index=False, sheet_name='Master Dictionary')
            wb = writer.book
            ws = writer.sheets['Master Dictionary']
            
            # FORMATS
            header_fmt = wb.add_format({'bold': True, 'fg_color': '#203764', 'font_color': 'white', 'border': 1, 'valign': 'top'})
            group_fmt = wb.add_format({'bold': True, 'bg_color': '#D9D9D9', 'valign': 'top', 'border': 1})
            attr_fmt = wb.add_format({'bold': True, 'valign': 'top', 'border': 1})
            user_fmt = wb.add_format({'bg_color': '#FFF2CC', 'text_wrap': True, 'valign': 'top', 'border': 1})
            text_fmt = wb.add_format({'text_wrap': True, 'valign': 'top', 'border': 1})
            
            # APPLIER
            ws.set_column('A:A', 25, group_fmt) # Entity Group
            ws.set_column('B:B', 30, attr_fmt)  # Attribute
            ws.set_column('C:C', 45, text_fmt)  # Auto Desc
            ws.set_column('D:D', 45, user_fmt)  # USER DESC
            ws.set_column('E:I', 15, text_fmt)  # Metadata
            ws.set_column('J:K', 40, text_fmt)  # Contexts

            # Headers & Freeze
            for col, val in enumerate(final_df.columns):
                ws.write(0, col, val, header_fmt)
            ws.freeze_panes(1, 0)
            ws.autofilter(0, 0, len(final_df), len(final_df.columns)-1)

    else:
        final_df.to_excel(OUTPUT_FILE, index=False)

    print("✅ Done. Ready for analysis.")

# --- ENTRY POINT ---

def main():
    if not os.path.isdir(INPUT_DIR):
        print(f"❌ Error: {INPUT_DIR} not found.")
        return
    
    files = glob.glob(os.path.join(INPUT_DIR, "*.json")) + glob.glob(os.path.join(INPUT_DIR, "*.yaml")) + glob.glob(os.path.join(INPUT_DIR, "*.yml"))
    
    if not files:
        print("❌ No files found.")
        return

    print(f"🚀 Processing {len(files)} files...")
    rows = process_files(files)
    generate_report(rows)

if __name__ == "__main__":
    main()