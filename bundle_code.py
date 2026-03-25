import os

# Files and extensions to exclude
EXT_EXCLUDES = {'.pdf', '.png', '.jpg', '.jpeg', '.gif', '.zip', '.tar', '.gz', '.db', '.pyc'}
DIR_EXCLUDES = {'.git', '__pycache__', '.venv', 'venv', 'brain', 'tmp', '.gemini', '.antigravity'}
FILE_EXCLUDES = {'.env', 'LICENSE', 'server_error.log', '_codebase_bundle.txt', 'bundle_code.py'}

def create_bundle(output_file='_codebase_bundle.txt'):
    with open(output_file, 'w', encoding='utf-8') as bundle:
        # First, add the directory structure
        bundle.write("================================================================\n")
        bundle.write("DIRECTORY STRUCTURE\n")
        bundle.write("================================================================\n\n")
        for root, dirs, files in os.walk('.'):
            # Prune directories
            dirs[:] = [d for d in dirs if d not in DIR_EXCLUDES]
            level = root.replace('.', '').count(os.sep)
            indent = '  ' * level
            bundle.write(f"{indent}{os.path.basename(root)}/\n")
            sub_indent = '  ' * (level + 1)
            for f in files:
                if f not in FILE_EXCLUDES and os.path.splitext(f)[1] not in EXT_EXCLUDES:
                    bundle.write(f"{sub_indent}{f}\n")
        
        bundle.write("\n\n")

        # Now, add the file contents
        for root, dirs, files in os.walk('.'):
            # Prune directories again
            dirs[:] = [d for d in dirs if d not in DIR_EXCLUDES]
            for f in sorted(files):
                if f in FILE_EXCLUDES or os.path.splitext(f)[1] in EXT_EXCLUDES:
                    continue
                
                rel_path = os.path.join(root, f)
                bundle.write("=" * 64 + "\n")
                bundle.write(f"FILE: {rel_path}\n")
                bundle.write("=" * 64 + "\n")
                
                try:
                    with open(rel_path, 'r', encoding='utf-8') as src:
                        content = src.read()
                        bundle.write(content)
                except UnicodeDecodeError:
                    bundle.write("[SKIPPED: Non-UTF8 or binary file format]")
                except Exception as e:
                    bundle.write(f"[ERROR READING FILE: {e}]")
                
                bundle.write("\n\n")

    print(f"Successfully bundled codebase into: {output_file}")

if __name__ == "__main__":
    create_bundle()
