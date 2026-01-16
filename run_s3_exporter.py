#!/usr/bin/env python3
"""
Script to export all files from a DNAnexus project to S3.
Gets all file IDs from a project and runs the aws_platform_to_s3_file_transfer app.
"""

import subprocess
import json
import sys
import argparse
from collections import defaultdict
import tempfile
import os

# Import dxpy and handle if not installed
try:
    import dxpy
except ImportError:
    print("Error: dxpy module not found. Please install it with: pip install dxpy")
    sys.exit(1)


def get_project_info(project_id):
    """Get the project name and archival state from project ID."""
    try:
        result = subprocess.run(
            ["dx", "describe", project_id, "--json"],
            capture_output=True,
            text=True,
            check=True
        )
# Parse JSON output
        project_data = json.loads(result.stdout)
        return {
            "name": project_data.get("name", ""),
            "archivalState": project_data.get("archivalState", "live")
        }
    except subprocess.CalledProcessError as e:
        print(f"Error getting project info: {e.stderr}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error parsing project data: {e}")
        sys.exit(1)


def get_file_details(project_id):
    """Get detailed information about all files in a DNAnexus project."""
    try:
        # Use dxpy API with describe=True for efficient bulk describe
        print("Fetching file details")
        
        detailed_files = []
        for item in dxpy.find_data_objects(
            project=project_id,
            classname='file',
            describe=True
        ):
            describe = item.get('describe', {})
            file_info = {
                "id": item['id'],
                "name": describe.get("name", ""),
                "folder": describe.get("folder", "/"),
                "size": describe.get("size", 0),
                "created": describe.get("created", ""),
                "archivalState": describe.get("archivalState", "live")
            }
            detailed_files.append(file_info)
        
        return detailed_files
    except Exception as e:
        print(f"Error finding files: {e}")
        sys.exit(1)


def unarchive_project(project_id):
    """Unarchive an entire project and all its files."""
    print(f"\nUnarchiving project {project_id}...")
    print("This operation will unarchive all files in the project.")
    
    try:
        # Use the format project-<ID>:/ to unarchive all files in the project
        result = subprocess.run(
            ["dx", "unarchive", f"{project_id}:/"],
            capture_output=True,
            text=True,
            check=True
        )
        print("\n✓ Unarchive request submitted successfully!")
        print("\nNote: The project and all files are now unarchiving. This process may take some time.")
        print("You can check the status with: dx describe " + project_id)
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Error unarchiving project: {e.stderr}")
        return False


def format_size(size_bytes):
    """Format file size in human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def identify_duplicates(files):
    """Identify files with duplicate names in same folder.
    
    Returns:
        dict: Maps file_id to (new_name, is_duplicate, file_info, duplicate_number)
    """
    folder_name_map = defaultdict(list)
    
    # Group files by (folder, name)
    for file_info in files:
        # Skip upload_report files
        if file_info['name'].startswith('upload_report'):
            continue
        key = (file_info['folder'], file_info['name'])
        folder_name_map[key].append(file_info)
    
    # Build rename plan
    rename_plan = {}
    duplicate_groups = 0
    
    for (folder, name), file_list in folder_name_map.items():
        if len(file_list) > 1:
            duplicate_groups += 1
            print(f"\n  Found {len(file_list)} files named '{name}' in folder '{folder}'")
            
            # Sort by creation date (oldest first)
            sorted_files = sorted(file_list, key=lambda f: f['created'])
            
            # First (oldest) file keeps original name, rest get incremental numbers
            for idx, file_info in enumerate(sorted_files):
                is_duplicate = (idx > 0)
                
                if is_duplicate:
                    new_name = generate_incremental_filename(name, idx)
                else:
                    new_name = name
                
                rename_plan[file_info['id']] = {
                    'new_name': new_name,
                    'is_duplicate': is_duplicate,
                    'file_info': file_info,
                    'duplicate_number': idx if is_duplicate else None
                }
                
                if idx == 0:
                    print(f"    {file_info['id']} (created: {file_info['created']}): Keep original name '{name}'")
                else:
                    print(f"    {file_info['id']} (created: {file_info['created']}): Will rename to '{new_name}'")
        else:
            # Unique file - no renaming needed
            file_info = file_list[0]
            rename_plan[file_info['id']] = {
                'new_name': file_info['name'],
                'is_duplicate': False,
                'file_info': file_info,
                'duplicate_number': None
            }
    
    return rename_plan, duplicate_groups


def generate_incremental_filename(original_name, number):
    """Generate filename with incremental number before extension.
    
    Examples:
        files_detected.txt.gz, 1 -> files_detected_1.txt.gz
        data.tar.gz, 2 -> data_2.tar.gz
        report.pdf, 1 -> report_1.pdf
    """
    if '.' in original_name:
        parts = original_name.rsplit('.', 1)
        base_name = parts[0]
        extension = parts[1]
        
        # Handle multi-part extensions like .tar.gz, .txt.gz
        if '.' in base_name:
            inner_parts = base_name.rsplit('.', 1)
            if inner_parts[1] in ['tar', 'txt', 'csv']:
                base_name = inner_parts[0]
                extension = f"{inner_parts[1]}.{extension}"
        
        return f"{base_name}_{number}.{extension}"
    else:
        return f"{original_name}_{number}"


def rename_file_in_dnanexus(file_id, new_name, project_id):
    """Rename a file in DNAnexus.
    
    Args:
        file_id: DNAnexus file ID
        new_name: New name for the file
        project_id: Project ID where the file exists
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        result = subprocess.run(
            ["dx", "rename", file_id, new_name, "--project", project_id],
            capture_output=True,
            text=True,
            check=True
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error renaming file {file_id}: {e.stderr}")
        return False


def get_s3_existing_files(s3_bucket, project_name):
    """Get list of existing files in S3 bucket for this project.
    
    Returns:
        set: Set of S3 keys (relative paths within project folder)
    """
    try:
        print(f"Checking S3 for existing files in {project_name}/...")
        result = subprocess.run(
            ["aws", "s3", "ls", f"s3://{s3_bucket}/{project_name}/", "--recursive"],
            capture_output=True,
            text=True,
            check=True
        )
        
        # Parse output to extract file paths
        # Format: "2024-01-01 12:00:00   1234 project_name/folder/file.txt"
        existing_files = set()
        for line in result.stdout.strip().split('\n'):
            if line.strip():
                parts = line.split()
                if len(parts) >= 4:
                    # Get the full path and extract relative path within project
                    full_path = ' '.join(parts[3:])
                    # Remove project name prefix to get relative path
                    if full_path.startswith(f"{project_name}/"):
                        relative_path = full_path[len(project_name)+1:]
                        existing_files.add(relative_path)
        
        print(f"✓ Found {len(existing_files)} existing files in S3")
        return existing_files
        
    except subprocess.CalledProcessError as e:
        if "NoSuchBucket" in e.stderr or "does not exist" in e.stderr:
            print("✓ No existing files found (bucket or path doesn't exist yet)")
            return set()
        else:
            print(f"Warning: Could not check S3: {e.stderr}")
            print("Proceeding with transfer of all files...")
            return set()
    except Exception as e:
        print(f"Warning: Error checking S3: {e}")
        print("Proceeding with transfer of all files...")
        return set()


def generate_transfer_mapping(project_id, output_file="transfer_mapping.json", files=None, project_info=None):
    """Generate a JSON mapping file showing original and renamed filenames.
    This helps track which files were renamed in DNAnexus.
    """
    if files is None:
        files = get_file_details(project_id)
    rename_plan, _ = identify_duplicates(files)
    
    if project_info is None:
        project_info = get_project_info(project_id)
    project_name = project_info["name"]
    
    mapping = {
        "project_id": project_id,
        "project_name": project_name,
        "files": []
    }
    
    for file_id, plan in rename_plan.items():
        file_info = plan['file_info']
        mapping["files"].append({
            "file_id": file_id,
            "original_name": file_info['name'],
            "new_name": plan['new_name'],
            "dx_folder": file_info['folder'],
            "dx_path": f"{file_info['folder']}/{plan['new_name']}",
            "is_duplicate": plan['is_duplicate'],
            "was_renamed": plan['is_duplicate'],
            "duplicate_number": plan['duplicate_number'],
            "created_date": file_info['created']
        })
    
    with open(output_file, 'w') as f:
        json.dump(mapping, f, indent=2)
    
    print(f"\n✓ Generated transfer mapping file: {output_file}")
    print(f"  This file tracks all renamed files in DNAnexus")
    
    return output_file


def show_dry_run(files, target_s3_bucket, config_file, verify_transfer, conserve_structure):
    """Display what files will be transferred and where."""
    if not files:
        print("No files found in the project.")
        return
    
    s3_base = f"s3://{target_s3_bucket}"
    
    print("\n" + "="*80)
    print("DRY RUN - Files to be transferred:")
    print("="*80)
    print(f"\nTotal files: {len(files)}")
    
    total_size = sum(f["size"] for f in files)
    print(f"Total size: {format_size(total_size)}")
    print(f"\nDestination: {s3_base}")
    print(f"Conserve structure: {conserve_structure}")
    print(f"Verify transfer: {verify_transfer}")
    print(f"Config file: {config_file}")
    print("\n" + "-"*80)
    
    for i, file_info in enumerate(files, 1):
        if conserve_structure:
            # Files will maintain their folder structure
            s3_dest = f"{s3_base}{file_info['folder']}/{file_info['name']}"
        else:
            # Files will be placed flat in the root
            s3_dest = f"{s3_base}/{file_info['name']}"
        
        archival_status = file_info.get('archivalState', 'live')
        status_indicator = "⚠️ " if archival_status != "live" else ""
        
        print(f"\n{i}. {status_indicator}{file_info['name']}")
        print(f"   ID: {file_info['id']}")
        print(f"   Size: {format_size(file_info['size'])}")
        print(f"   State: {archival_status}")
        print(f"   DNAnexus path: {file_info['folder']}/{file_info['name']}")
        print(f"   S3 destination: {s3_dest}")
    
    print("\n" + "="*80)
    print(f"\nJSON input file that will be created:")
    file_ids = [f["id"] for f in files]
    
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_filename = f"dx_input_{timestamp}.json"
    
    input_spec = {
        "f_ids": file_ids,
        "target_s3": s3_base,
        "config_file": {"$dnanexus_link": config_file},
        "verify_transfer": verify_transfer,
        "conserve_structure": conserve_structure
    }
    
    print(f"\nFilename: {json_filename}")
    print(json.dumps(input_spec, indent=2))
    
    print(f"\nCommand that will be executed:")
    print(f'dx run app-J2v4yBQ0yBByQ6g9gPqbpY4q -f {json_filename} --brief -y')
    print("\n" + "="*80)


def run_s3_exporter(file_ids, target_s3_bucket, config_file, verify_transfer=True, conserve_structure=True, dry_run=False, project_id=None, handle_duplicates=True, skip_existing=False, files=None, project_info=None):
    """Run the aws_platform_to_s3_file_transfer app with the given file IDs."""
    if not file_ids:
        print("No files found in the project.")
        return
    
    # Use provided project_info or fetch if not provided
    if project_info is None:
        project_info = get_project_info(project_id)
    project_name = project_info["name"]
    
    # Check for existing files in S3 if skip_existing is enabled
    existing_files_in_s3 = set()
    if skip_existing:
        existing_files_in_s3 = get_s3_existing_files(target_s3_bucket, project_name)
    
    # Handle duplicate filenames if requested
    if handle_duplicates:
        print("\nChecking for duplicate filenames...")
        # Use provided files or fetch if not provided
        if files is None:
            files = get_file_details(project_id)
        rename_plan, duplicate_groups = identify_duplicates(files)
        
        if duplicate_groups > 0:
            duplicate_file_count = sum(1 for v in rename_plan.values() if v['is_duplicate'])
            total_in_duplicate_groups = sum(1 for v in rename_plan.values() 
                                           if any(v2['file_info']['folder'] == v['file_info']['folder'] and 
                                                 v2['file_info']['name'] == v['file_info']['name'] and 
                                                 len([x for x in rename_plan.values() 
                                                     if x['file_info']['folder'] == v['file_info']['folder'] and 
                                                        x['file_info']['name'] == v['file_info']['name']]) > 1
                                                 for v2 in rename_plan.values()))
            
            print(f"\n✓ Found {duplicate_groups} duplicate filename groups")
            print(f"  {total_in_duplicate_groups} total files involved in duplicates")
            print(f"  {duplicate_file_count} will be renamed in DNAnexus (oldest file keeps original name)")
            
            if dry_run:
                # Separate files that will be renamed vs those that keep original names
                files_to_rename = [(file_id, plan) for file_id, plan in rename_plan.items() if plan['is_duplicate']]
                files_keeping_original = [(file_id, plan) for file_id, plan in rename_plan.items() if not plan['is_duplicate']]
                
                print("\n[DRY RUN] Duplicate file groups found:")
                print("="*80)
                
                # First show files that will keep their original names (oldest in each group)
                if files_keeping_original:
                    print("\nFiles keeping ORIGINAL names (oldest in each duplicate group):")
                    print("-"*80)
                    for file_id, plan in files_keeping_original:
                        file_info = plan['file_info']
                        print(f"\n  File: {file_info['folder']}/{file_info['name']}")
                        print(f"    ID: {file_id}")
                        print(f"    Created: {file_info['created']}")
                        print(f"    Status: Will keep original name")
                
                # Then show files that will be renamed (shown last as requested)
                if files_to_rename:
                    print("\n" + "="*80)
                    print("\nFiles that will be RENAMED in DNAnexus:")
                    print("-"*80)
                    for file_id, plan in files_to_rename:
                        file_info = plan['file_info']
                        print(f"\n  Original: {file_info['folder']}/{file_info['name']}")
                        print(f"    ID: {file_id}")
                        print(f"    Created: {file_info['created']}")
                        print(f"    New name: {plan['new_name']}")
                        print(f"    Number in sequence: _{plan['duplicate_number']}")
                
                print("\n" + "="*80)
                print(f"\nSummary:")
                print(f"  Total duplicate groups: {duplicate_groups}")
                print(f"  Files keeping original names: {len(files_keeping_original)}")
                print(f"  Files to be renamed: {len(files_to_rename)}")
                print("\nNote: Oldest files in each group keep their original names")
                return  # Exit after showing dry-run for duplicates
            else:
                # Show user what will be renamed and ask for confirmation
                print("\n" + "="*80)
                print("Files to be renamed in DNAnexus:")
                print("="*80)
                
                duplicates_to_rename = [(file_id, plan) for file_id, plan in rename_plan.items() if plan['is_duplicate']]
                
                for file_id, plan in duplicates_to_rename:
                    file_info = plan['file_info']
                    print(f"\n  Original: {file_info['folder']}/{file_info['name']}")
                    print(f"    Created: {file_info['created']}")
                    print(f"    New name: {plan['new_name']}")
                    print(f"    File ID: {file_id}")
                
                print("\n" + "="*80)
                print(f"Total files to rename: {len(duplicates_to_rename)}")
                print("Note: Files will ONLY be renamed in DNAnexus storage")
                print("      Oldest file in each group keeps its original name")
                print("="*80)
                
                # Ask for user confirmation
                response = input("\nProceed with renaming these files in DNAnexus? (yes/y to confirm): ").strip().lower()
                
                if response not in ['yes', 'y']:
                    print("\n❌ Renaming cancelled by user. No files were modified.")
                    return
                
                # Rename duplicates in DNAnexus
                print(f"\n✓ Confirmed. Renaming duplicate files in DNAnexus...")
                print("Note: Only duplicates will be renamed (oldest keeps original name)\n")
                
                renamed_count = 0
                failed_renames = 0
                
                for file_id, plan in duplicates_to_rename:
                    file_info = plan['file_info']
                    old_name = file_info['name']
                    new_name = plan['new_name']
                    
                    print(f"  Renaming: {old_name} -> {new_name}...", end='', flush=True)
                    
                    if rename_file_in_dnanexus(file_id, new_name, project_id):
                        print(" ✓")
                        renamed_count += 1
                        # Update the file_info with new name for transfer
                        plan['file_info']['name'] = new_name
                    else:
                        print(" ✗")
                        failed_renames += 1
                
                print(f"\n✓ Renamed {renamed_count} duplicate files in DNAnexus")
                if failed_renames > 0:
                    print(f"⚠️  Failed to rename {failed_renames} files")
                    print(f"⚠️  Stopping transfer due to rename failures")
                    return
                
                # Generate transfer mapping file and upload to DNAnexus
                mapping_filename = f"{project_name}_transfer_mapping.json"
                print(f"\nGenerating transfer mapping file: {mapping_filename}...")
                generate_transfer_mapping(
                    project_id=project_id,
                    output_file=mapping_filename,
                    files=files,
                    project_info=project_info
                )
                
                # Upload mapping file to DNAnexus project
                print(f"Uploading transfer mapping to DNAnexus project...")
                try:
                    upload_cmd = [
                        "dx", "upload", mapping_filename,
                        "--destination", f"{project_id}:/",
                        "--brief"
                    ]
                    result = subprocess.run(upload_cmd, check=True, capture_output=True, text=True)
                    mapping_file_id = result.stdout.strip()
                    print(f"✓ Transfer mapping uploaded to DNAnexus: {mapping_file_id}")
                except subprocess.CalledProcessError as e:
                    print(f"⚠️  Failed to upload mapping file to DNAnexus: {e.stderr}")
                    print(f"   Mapping file saved locally: {mapping_filename}")
                
                # Files are now renamed in DNAnexus, get updated file IDs for transfer
                # The DNAnexus S3 exporter will use the new names automatically
                file_ids = list(rename_plan.keys())
                
                print(f"\n✓ All duplicate files renamed successfully in DNAnexus")
                print(f"Now transferring {len(file_ids)} files directly to S3 using DNAnexus S3 exporter...")
                print("Note: Files will be transferred with their current names in DNAnexus\n")
                
                # Continue to standard DNAnexus S3 exporter (below)
        else:
            print("\n✓ No duplicate filenames found - all files have unique names")
            print("  Will use standard DNAnexus S3 exporter app")
    
    # Filter out existing files if skip_existing is enabled
    if skip_existing and existing_files_in_s3:
        if files is None:
            files = get_file_details(project_id)
        
        original_count = len(file_ids)
        files_to_skip = []
        files_to_transfer = []
        
        for file_info in files:
            if file_info['id'] in file_ids:
                # Construct the S3 path for this file
                folder = file_info['folder'].lstrip('/')
                if folder:
                    s3_relative_path = f"{folder}/{file_info['name']}"
                else:
                    s3_relative_path = file_info['name']
                
                # Check if this file already exists in S3
                if s3_relative_path in existing_files_in_s3:
                    files_to_skip.append(file_info)
                else:
                    files_to_transfer.append(file_info['id'])
        
        file_ids = files_to_transfer
        
        print(f"\n--skip-existing enabled:")
        print(f"  Total files in DNAnexus: {original_count}")
        print(f"  Already in S3 (skipping): {len(files_to_skip)}")
        print(f"  New files to transfer: {len(file_ids)}")
        
        if not file_ids:
            print("\n✓ All files already exist in S3. Nothing to transfer.")
            return
    
    if dry_run:
        print("\n[DRY RUN MODE] The transfer job will NOT be executed.")
        return
    
    # Select the project to ensure the job runs in the correct context
    if project_id:
        print(f"Selecting project {project_id}...")
        try:
            subprocess.run(["dx", "select", project_id], check=True, capture_output=True, text=True)
            print("✓ Project selected successfully")
        except subprocess.CalledProcessError as e:
            print(f"Error selecting project: {e.stderr}")
            sys.exit(1)
    
    # Create temporary JSON file with timestamped name
    import tempfile
    import os
    from datetime import datetime
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_dir = tempfile.gettempdir()
    json_filename = f"dx_input_{timestamp}.json"
    json_filepath = os.path.join(temp_dir, json_filename)
    
    # Build input JSON structure with DNAnexus links
    # Note: target_s3 expects just the bucket name, not the full s3:// path
    input_spec = {
        "f_ids": [{"$dnanexus_link": fid} for fid in file_ids],
        "target_s3": target_s3_bucket,
        "config_file": {"$dnanexus_link": config_file},
        "verify_transfer": verify_transfer,
        "conserve_structure": conserve_structure
    }
    
    # Write to JSON file
    with open(json_filepath, 'w') as f:
        json.dump(input_spec, f, indent=2)
    
    # Construct display path for user feedback
    s3_display_path = f"s3://{target_s3_bucket}"
    print(f"Created input file: {json_filepath}")
    print(f"Exporting {len(file_ids)} files to {s3_display_path}")
    
    try:
        # Build the dx run command using -f flag to read from JSON file
        cmd = [
            "dx", "run", "app-J2v4yBQ0yBByQ6g9gPqbpY4q",
            "-f", json_filepath,
            "--project", project_id,  # Explicitly specify the project to avoid running in wrong project
            "--brief", "-y"
        ]
        
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        job_id = result.stdout.strip()
        print(f"\n✓ Successfully started S3 export job!")
        print(f"Job ID: {job_id}")
        print(f"\nMonitor the job with:")
        print(f"  dx watch {job_id}")
        print(f"  dx describe {job_id}")
        print(f"\nInput file saved at: {json_filepath}")
    except subprocess.CalledProcessError as e:
        print(f"Error running S3 exporter: {e.stderr}")
        print(f"\nInput file saved at: {json_filepath}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Export all files from a DNAnexus project to S3"
    )
    parser.add_argument(
        "--project-id",
        required=True,
        help="DNAnexus project ID (e.g., project-XXX)"
    )
    parser.add_argument(
        "--s3-bucket",
        required=True,
        help="Target S3 bucket name (without s3:// prefix)"
    )
    parser.add_argument(
        "--config-file",
        default="file-J4v10000Z0gjXxVPZB3vvYqv",
        help="AWS config file ID (default: file-J4v10000Z0gjXxVPZB3vvYqv)"
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Disable transfer verification"
    )
    parser.add_argument(
        "--no-conserve-structure",
        action="store_true",
        help="Disable conserving directory structure"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what files will be transferred without executing the transfer"
    )
    parser.add_argument(
        "--unarchive",
        action="store_true",
        help="Automatically unarchive any archived files before transfer"
    )
    parser.add_argument(
        "--no-handle-duplicates",
        action="store_true",
        help="Disable automatic renaming of duplicate filenames (may cause data loss)"
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files that already exist in S3 (checks S3 bucket before transfer)"
    )
    
    args = parser.parse_args()
    
    # Get project info including archival state
    project_info = get_project_info(args.project_id)
    project_name = project_info["name"]
    project_archival_state = project_info["archivalState"]
    
    print(f"Project: {project_name} ({args.project_id})")
    print(f"Archival state: {project_archival_state}")
    
    # Check if project is archived
    if project_archival_state == "archived":
        print("\n⚠️  WARNING: This project is ARCHIVED.")
        print("All files in an archived project must be unarchived before transfer.\n")
        
        if args.unarchive:
            # Unarchive without prompting if --unarchive flag is set
            print("--unarchive flag detected. Proceeding to unarchive the project...")
            if unarchive_project(args.project_id):
                print("\n⏳ Project unarchive request submitted.")
                print("The script will now check if files are fully unarchived...")
                print("This may take a moment as the system processes the unarchive request.\n")
                # Don't exit - continue to check file states below
            else:
                sys.exit(1)
        else:
            # Prompt user
            response = input("Would you like to unarchive this project? (yes/no): ").strip().lower()
            if response in ["yes", "y"]:
                if unarchive_project(args.project_id):
                    print("\n⏳ Project unarchive request submitted.")
                    print("The script will now check if files are fully unarchived...")
                    print("This may take a moment as the system processes the unarchive request.\n")
                    # Don't exit - continue to check file states below
                else:
                    sys.exit(1)
            else:
                print("\n❌ Cannot proceed with transfer while project is archived.")
                print("To unarchive later, run: dx unarchive " + args.project_id)
                print("Or run this script with --unarchive flag.")
                sys.exit(1)
    
    elif project_archival_state == "unarchiving":
        print("\n⚠️  WARNING: This project is currently UNARCHIVING.")
        print("The script will check if individual files are ready for transfer...")
        # Don't exit - continue to check file states below
    
    elif project_archival_state == "archiving":
        print("\n⚠️  WARNING: This project is currently ARCHIVING.")
        print("Cannot transfer data while the project is being archived.")
        sys.exit(1)
    
    else:
        print("✓ Project is live and ready for transfer.\n")
    
    # Get all file details from the project (fetch once)
    print(f"Finding all files in project...")
    files = get_file_details(args.project_id)
    print(f"Found {len(files)} files")
    file_ids = [f['id'] for f in files]
    
    if args.dry_run:
        # Check if we should handle duplicates in dry-run mode
        if not args.no_handle_duplicates:
            # Run duplicate detection and show results
            run_s3_exporter(
                file_ids=file_ids,
                target_s3_bucket=args.s3_bucket,
                config_file=args.config_file,
                verify_transfer=not args.no_verify,
                conserve_structure=not args.no_conserve_structure,
                dry_run=True,
                project_id=args.project_id,
                handle_duplicates=True,
                skip_existing=args.skip_existing,
                files=files,
                project_info=project_info
            )
        else:
            # Show standard dry-run output
            show_dry_run(
                files=files,
                target_s3_bucket=args.s3_bucket,
                config_file=args.config_file,
                verify_transfer=not args.no_verify,
                conserve_structure=not args.no_conserve_structure
            )
    else:
        # Check archival status before transfer
        print("\nChecking archival status of files...")
        archived_files = [f for f in files if f.get('archivalState', 'live') not in ['live', None]]
        
        if archived_files:
            # Count files by state
            unarchiving_files = [f for f in archived_files if f.get('archivalState') == 'unarchiving']
            actually_archived = [f for f in archived_files if f.get('archivalState') == 'archived']
            
            print(f"\n⚠️  WARNING: {len(archived_files)} file(s) are not yet live:")
            if unarchiving_files:
                print(f"  - {len(unarchiving_files)} file(s) currently UNARCHIVING")
            if actually_archived:
                print(f"  - {len(actually_archived)} file(s) still ARCHIVED")
            
            print("\nSample files:")
            for f in archived_files[:10]:
                print(f"  - {f['name']} (ID: {f['id']}, State: {f['archivalState']})")
            if len(archived_files) > 10:
                print(f"  ... and {len(archived_files) - 10} more")
            
            if args.unarchive:
                # Unarchive the entire project to ensure all files are included
                print("\n--unarchive flag detected. Unarchiving entire project...")
                if unarchive_project(args.project_id):
                    print("\n⏳ Waiting for all files to unarchive...")
                    print("Please run this script again once unarchiving completes.")
                    print(f"\nTo check status: dx describe {args.project_id}")
                    sys.exit(0)
                else:
                    sys.exit(1)
            else:
                # Prompt user
                print("\nThe unarchiving process is in progress. Please wait for it to complete.")
                response = input("\nWould you like to unarchive the entire project now? (yes/no): ").strip().lower()
                if response in ["yes", "y"]:
                    if unarchive_project(args.project_id):
                        print("\n⏳ Waiting for all files to unarchive...")
                        print("Please run this script again once unarchiving completes.")
                        print(f"\nTo check status: dx describe {args.project_id}")
                        sys.exit(0)
                    else:
                        sys.exit(1)
                else:
                    print("\n❌ Cannot proceed with transfer while files are not live.")
                    print(f"To unarchive manually: dx unarchive {args.project_id}:/")
                    print("Then run this script again once complete.")
                    sys.exit(1)
        else:
            print("✓ All files are live and ready for transfer\n")
        
        # Run the S3 exporter
        run_s3_exporter(
            file_ids=file_ids,
            target_s3_bucket=args.s3_bucket,
            config_file=args.config_file,
            verify_transfer=not args.no_verify,
            conserve_structure=not args.no_conserve_structure,
            dry_run=args.dry_run,
            project_id=args.project_id,
            handle_duplicates=not args.no_handle_duplicates,
            skip_existing=args.skip_existing,
            files=files,
            project_info=project_info
        )


if __name__ == "__main__":
    main()
