import yaml
import os

def load_yaml_files(directory):
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".yaml"):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r') as f:
                        yaml.safe_load(f)
                    print(f"SUCCESS: {path}")
                except Exception as e:
                    print(f"ERROR: {path} - {e}")

if __name__ == "__main__":
    load_yaml_files(r"c:\Users\Administrator\Projects\LLDM")
