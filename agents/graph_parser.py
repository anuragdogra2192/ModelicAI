import os
import re

class ModelicaGraphParser:
    def __init__(self):
        pass

    def parse_mo_file(self, file_path: str) -> dict:
        """Parses a Modelica .mo file to extract components and connection equations,

        transforming them into a React Flow compatible JSON schema.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Modelica file not found: {file_path}")

        with open(file_path, "r") as f:
            code = f.read()

        nodes = []
        edges = []

        # 1. Extract component declarations (e.g., "Modelica.Electrical.Analog.Basic.Resistor R1(R = 100);")
        component_pattern = re.compile(
            r'([\w\.]+)\s+(\w+)\s*(?:\((.*?)\))?;', re.DOTALL
        )
        
        # Simple auto-layout coordinate offsets
        x_offset, y_offset = 100, 200
        spacing = 220

        matches = component_pattern.findall(code)
        valid_node_count = 0

        for comp_type, instance_name, params in matches:
            # Filter out non-component keywords or imports
            if comp_type in ["equation", "algorithm", "model", "end", "class"] or "import" in comp_type:
                continue

            # Skip sub-elements or declarations inside components if any
            if "." in instance_name:
                continue

            short_type = comp_type.split(".")[-1].lower()

            nodes.append({
                "id": instance_name,
                "type": short_type,  # e.g., "resistor", "capacitor", "constantvoltage"
                "position": {
                    "x": x_offset + (valid_node_count * spacing), 
                    "y": y_offset
                },
                "data": {
                    "label": instance_name,
                    "componentType": comp_type,
                    "parameters": params.strip() if params else ""
                }
            })
            valid_node_count += 1

        # 2. Extract connection equations (e.g., "connect(V1.p, R1.n);")
        connect_pattern = re.compile(r'connect\s*\(\s*([\w\.]+)\s*,\s*([\w\.]+)\s*\)\s*;')
        connect_matches = connect_pattern.findall(code)

        for idx, (pin1, pin2) in enumerate(connect_matches):
            try:
                source_comp, source_pin = pin1.split(".", 1)
                target_comp, target_pin = pin2.split(".", 1)

                edges.append({
                    "id": f"e-{source_comp}-{target_comp}-{idx}",
                    "source": source_comp,
                    "target": target_comp,
                    "sourceHandle": source_pin,
                    "targetHandle": target_pin,
                    "type": "smoothstep"
                })
            except ValueError:
                continue

        return {
            "nodes": nodes,
            "edges": edges
        }