# DNAnexus to S3 Exporter

A toolkit for exporting files from DNAnexus projects to AWS S3 buckets with integrity verification.

## Overview

This repository contains two main tools:
1. **run_s3_exporter.py** - Python script to export files from DNAnexus to S3
2. **integrity_check.sh** - Bash script to verify transfer integrity and completeness

## Requirements

- DNAnexus CLI (`dx-toolkit`) installed and configured
- AWS CLI installed and configured
- Python 3.x with `dxpy` module
- `jq` command-line JSON processor
- Active DNAnexus session (`dx login`) which should be your personal login for auditability

### Installation

```bash
# Install dxpy
pip install dxpy

# Install jq (Ubuntu/Debian)
sudo apt-get install jq

# Install AWS CLI (if not already installed)
pip install awscli
```

## Configuration

### AWS Credentials

Create a `.env.secrets` file in the project directory with your AWS credentials:

```bash
export AWS_ACCESS_KEY_ID="your-access-key"
export AWS_SECRET_ACCESS_KEY="your-secret-key"
export AWS_DEFAULT_REGION="eu-west-2"
```

**Important:** Add `.env.secrets` to your `.gitignore` to avoid committing credentials.

## Usage

### 1. Exporting Files to S3

The `run_s3_exporter.py` script transfers files from a DNAnexus project to an S3 bucket.

#### Basic Usage

```bash
python3 run_s3_exporter.py --project-id project-XXX --s3-bucket your-bucket-name
```

#### Command-Line Options

| Option | Description |
|--------|-------------|
| `--project-id` | DNAnexus project ID (required, e.g., `project-XXX`) |
| `--s3-bucket` | Target S3 bucket name without `s3://` prefix (required) |
| `--config-file` | AWS config file ID (stored in an authentication project in DNAnexus) |
| `--dry-run` | Preview files to be transferred without executing |
| `--no-verify` | Disable transfer verification |
| `--no-conserve-structure` | Don't preserve directory structure in S3 |
| `--unarchive` | Automatically unarchive archived files before transfer |
| `--no-handle-duplicates` | Disable automatic renaming of duplicate filenames |
| `--skip-existing` | Skip files that already exist in S3 |

#### Examples

**Preview transfer (dry run):**
```bash
python3 run_s3_exporter.py \
    --project-id project-ABC123XYZ \
    --s3-bucket my-archive-bucket \
    --dry-run
```

**Transfer with automatic unarchiving:**
```bash
python3 run_s3_exporter.py \
    --project-id project-ABC123XYZ \
    --s3-bucket my-archive-bucket \
    --unarchive
```

#### Features

- **Duplicate Handling**: Automatically detects and renames duplicate filenames with incremental numbers
- **Archive Management**: Detects archived projects/files and prompts for unarchiving
- **Transfer Mapping**: Creates a JSON mapping file tracking original and renamed files, then uploads it to the DNAnexus project for permanent record-keeping
- **Resume Support**: Can skip files already in S3 with `--skip-existing`

### 2. Verifying Transfer Integrity

The `integrity_check.sh` script validates that all files were successfully transferred to S3.

#### Basic Usage

```bash
bash integrity_check.sh project-XXX
```

#### What It Does

1. **Compares file counts** between DNAnexus and S3
2. **Analyzes upload reports** to identify failed transfers
3. **Verifies actual file presence** in S3 bucket
4. **Handles duplicate files** using transfer mapping file (if present)
5. **Generates verification report** with detailed statistics
6. **Excludes metadata files** (upload reports and transfer mapping files) from data file counts



## Workflow

Recommended workflow for transferring a DNAnexus project:

1. **Preview the transfer:**
   ```bash
   python3 run_s3_exporter.py --project-id project-XXX --s3-bucket bucket-name --dry-run
   ```

2. **Execute the transfer:**
   ```bash
   python3 run_s3_exporter.py --project-id project-XXX --s3-bucket bucket-name --unarchive
   ```

3. **Monitor the job:**
   ```bash
   dx watch job-XXX
   ```
   Or check the project's monitor section on the DNAnexus website

4. **Verify the transfer:**
   ```bash
   source .env.secrets
   bash integrity_check.sh project-XXX
   ```

5. **Review the verification report:**
   ```bash
   cat {project_name}_verification_report.txt
   ```

## Important Notes

### Duplicate Files

When the script detects files with identical names in the same folder:
- The **oldest file** (by creation date) keeps the original name
- **Newer duplicates** are renamed with incremental numbers: `filename_1.ext`, `filename_2.ext`
- A transfer mapping file is created locally and uploaded to the DNAnexus project: `{project_name}_transfer_mapping.json`
- This mapping is used by `integrity_check.sh` for accurate verification
- The mapping file is excluded from file counts (metadata, not data)

### Archived Files

If a project or files are archived:
- The script will detect and prompt for unarchiving
- Use `--unarchive` flag to automatically unarchive
- Unarchiving may take up to 24hrs; return after 24hrs to rerun the script 

## Files Generated

- `{project_name}_transfer_mapping.json` - Maps DNAnexus files to S3 keys (with duplicate handling), stored locally and uploaded to the DNAnexus project
- `{project_name}_verification_report.txt` - Detailed verification summary including full list of missing files if any exist (generated by integrity_check.sh)
- `dx_input_{timestamp}.json` - Input specification for the transfer job (in temp directory)
- `upload_report_{timestamp}.txt` - Transfer report generated by the S3 exporter app (stored in DNAnexus project)

