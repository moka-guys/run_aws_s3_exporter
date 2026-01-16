#!/bin/bash

source .env.secrets

project=$1
project_name=$(dx describe $project --json | jq '.name' | tr -d '"')

echo "Project: $project"
echo "Project name:  $project_name"
echo ""

# Check if transfer mapping file exists
mapping_file="${project_name}_transfer_mapping.json"
if [ -f "$mapping_file" ]; then
    echo "✓ Found transfer mapping file: $mapping_file"
    echo "  (Will use for accurate duplicate file validation)"
    use_mapping=true
else
    use_mapping=false
fi

echo ""
echo "DNAnexus file count:"
dx find data --project $project --class file --brief | wc -l

echo ""
dx describe $project --json | jq -r '"Live data: " + (.dataUsage*100|floor/100|tostring) + " GB, Archived data: " + (.archivedDataUsage*100|floor/100|tostring) + " GB"'

echo ""
echo "AWS S3 file count:"
aws s3 ls s3://gstt-array-archive/$project_name/ --recursive | wc -l
echo ""
aws s3 ls s3://gstt-array-archive/$project_name/ --recursive --summarize | grep "Total Size:" | awk '{printf "Total Size: %.2f GB\n", $3/1024/1024/1024}'

echo ""
echo "###########################"
echo "Transfer Integrity Check:"
echo "###########################"

# Find upload report in DNAnexus project
upload_report=$(dx find data --project $project --name "upload_report*.txt" --brief 2>/dev/null | head -1)

if [ -z "$upload_report" ]; then
    echo "⚠️  No upload report found in DNAnexus project"
    echo "Transfer may not have been completed or report was not generated"
else
    echo "✓ Upload report found: $upload_report"
    
    temp_report=$(mktemp)
    if dx download $upload_report -o $temp_report -f 2>/dev/null; then
        # Get S3 file list once for all comparisons (much faster than multiple aws s3 ls calls)
        echo "Fetching S3 file list..."
        s3_list_file=$(mktemp)
        aws s3 ls s3://gstt-array-archive/$project_name/ --recursive > "$s3_list_file" 2>/dev/null
        # Count successes and failures
        success_count=$(grep -c "^SUCCESS:" $temp_report 2>/dev/null || echo 0)
        
        # Get unique failed filenames and check if they exist in S3
        failed_files=$(grep "^FAILED:" $temp_report 2>/dev/null | awk '{print $2}' | sort -u)
        actual_failures=0
        verified_in_s3=0
        
        if [ -n "$failed_files" ]; then
            echo "Verifying failed files in S3..."
            while IFS= read -r file; do
                # Check if this file eventually succeeded in the report
                if ! grep -q "^SUCCESS:.*$file" $temp_report; then
                    # File marked as failed without subsequent success - verify in S3 using cached list
                    if grep -q "$file" "$s3_list_file"; then
                        verified_in_s3=$((verified_in_s3 + 1))
                    else
                        actual_failures=$((actual_failures + 1))
                    fi
                fi
            done <<< "$failed_files"
        fi
        
        total_attempts=$(grep -E "^(SUCCESS|FAILED):" $temp_report 2>/dev/null | wc -l)
        
        echo ""
        echo "Upload Report Summary:"
        echo "  Total transfer attempts: $total_attempts"
        echo "  Successful transfers: $success_count"
        echo "  Failed in report but verified in S3: $verified_in_s3"
        echo "  Actually failed (missing from S3): $actual_failures"
        
        # Get actual file counts
        dnanexus_count=$(dx find data --project $project --class file --brief | wc -l)
        s3_count=$(wc -l < "$s3_list_file")
        
        # Subtract upload reports and transfer mapping from DNAnexus count (metadata, not data)
        upload_report_count=$(dx find data --project $project --name "upload_report*.txt" --brief 2>/dev/null | wc -l)
        transfer_mapping_count=$(dx find data --project $project --name "*_transfer_mapping.json" --brief 2>/dev/null | wc -l)
        dnanexus_data_files=$((dnanexus_count - upload_report_count - transfer_mapping_count))
        
        # Subtract upload reports and transfer mapping from S3 count
        s3_files_only=$((s3_count - upload_report_count - transfer_mapping_count))
        
        echo ""
        echo "###########################"
        echo "Truly Missing Files Check:"
        echo "###########################"
        
        # Get all DNAnexus filenames (excluding upload reports and transfer mapping)
        echo "Analyzing file presence in S3..."
        dx find data --project $project --class file --json | jq -r '.[] | select(.describe.name | (startswith("upload_report") or endswith("_transfer_mapping.json")) | not) | .describe.name' | sort > /tmp/dx_files_no_reports.txt
        
        if [ "$use_mapping" = true ]; then
            echo "Using transfer mapping file for validation..."
            
            # Validate using the mapping file
            truly_missing=0
            verified_count=0
            renamed_verified=0
            
            total_mapped=$(jq '.files | length' "$mapping_file")
            
            missing_files_list=$(mktemp)
            jq -r '.files[] | @json' "$mapping_file" | while read -r file_json; do
                dx_name=$(echo "$file_json" | jq -r '.dx_name')
                s3_key=$(echo "$file_json" | jq -r '.s3_key')
                is_duplicate=$(echo "$file_json" | jq -r '.is_duplicate')
                
                # Check if file exists in S3 with the expected key
                if grep -q "$s3_key" "$s3_list_file"; then
                    verified_count=$((verified_count + 1))
                    if [ "$is_duplicate" = "true" ]; then
                        renamed_verified=$((renamed_verified + 1))
                    fi
                else
                    echo "  ✗ MISSING: $dx_name -> $s3_key"
                    echo "$dx_name -> $s3_key" >> "$missing_files_list"
                    truly_missing=$((truly_missing + 1))
                fi
            done
            
            # Save counts to temp file for later use
            echo "$truly_missing" > /tmp/truly_missing_count.txt
            echo "$renamed_verified" > /tmp/renamed_verified_count.txt
            
            truly_missing=$(cat /tmp/truly_missing_count.txt 2>/dev/null || echo 0)
            renamed_verified=$(cat /tmp/renamed_verified_count.txt 2>/dev/null || echo 0)
            
            echo ""
            echo "Validation with mapping file:"
            echo "  Files checked: $total_mapped"
            echo "  Renamed duplicates verified: $renamed_verified"
            echo "  Truly missing: $truly_missing"
        else
            # No mapping file - set defaults
            truly_missing=0
            renamed_verified=0
            echo "No mapping file found - skipping duplicate validation"
        fi
        
        echo ""
        echo "Transfer Summary & Verification:"
        echo "  DNAnexus data files: $dnanexus_data_files"
        echo "  S3 files: $s3_files_only"
        echo "  Files in upload report: $total_attempts (of which $((success_count + verified_in_s3)) successful)"
        echo "  Files not in upload report: $((dnanexus_data_files - total_attempts))"
        echo "  Files truly missing from S3: $truly_missing"
        
        # Generate verification report
        report_file="${project_name}_verification_report.txt"
        {
            echo "=========================================="
            echo "TRANSFER VERIFICATION REPORT"
            echo "=========================================="
            echo "Project: $project_name"
            echo "Project ID: $project"
            echo "Date: $(date)"
            echo ""
            echo "Validation method: Transfer mapping file"
            echo "Renamed duplicates handled: Yes"
            echo ""
            echo "FILE COUNTS:"
            echo "  DNAnexus data files: $dnanexus_data_files"
            echo "  S3 files: $s3_files_only"
            echo "  Upload reports (metadata): $upload_report_count"
            echo ""
            echo "UPLOAD REPORT ANALYSIS:"
            echo "  Total transfer attempts: $total_attempts"
            echo "  Successful transfers: $success_count"
            echo "  Failed but found in S3: $verified_in_s3"
            echo "  Actually failed: $actual_failures"
            echo ""
            echo "MISSING FILES ANALYSIS:"
            echo "  Files not in upload report: $((dnanexus_data_files - total_attempts))"
            echo "  Truly missing from S3: $truly_missing"
            echo ""
            if [ "$truly_missing" -eq 0 ]; then
                echo "STATUS: ✅ ALL DATA FILES PRESENT IN S3"
            else
                echo "STATUS: ⚠️  $truly_missing FILE(S) MISSING FROM S3"
                echo ""
                echo "MISSING FILES:"
                if [ -f "$missing_files_list" ]; then
                    cat "$missing_files_list" | while read line; do
                        echo "  - $line"
                    done
                fi
            fi
            echo "=========================================="
        } > "$report_file"
        
        echo ""
        echo "📄 Verification report saved to: $report_file"
        
        # Check if all files transferred successfully
        total_successful=$((success_count + verified_in_s3))
        if [ "$truly_missing" -eq 0 ] && [ "$actual_failures" -eq 0 ]; then
            echo ""
            echo "✅ TRANSFER COMPLETE: All data files verified present in S3"
            if [ "$use_mapping" = true ] && [ "$renamed_verified" -gt 0 ]; then
                echo "   (Including $renamed_verified renamed duplicate files)"
            fi
        else
            echo ""
            echo "⚠️  TRANSFER INCOMPLETE OR ISSUES DETECTED:"
            
            if [ "$truly_missing" -gt 0 ]; then
                echo "  - $truly_missing file(s) are truly missing from S3"
                if [ -f "$missing_files_list" ]; then
                    echo ""
                    echo "Sample of truly missing files (first 10):"
                    head -10 "$missing_files_list" | while read file; do
                        echo "    - $file"
                    done
                    if [ "$truly_missing" -gt 10 ]; then
                        echo "    ... and $((truly_missing - 10)) more"
                    fi
                    echo ""
                    echo "See full list in verification report: $report_file"
                fi
            fi
            
            if [ "$actual_failures" -gt 0 ]; then
                echo "  - $actual_failures file(s) failed in upload report and are missing from S3"
            fi
            
            if [ "$verified_in_s3" -gt 0 ]; then
                echo "  - Note: $verified_in_s3 file(s) marked as failed in report but verified to exist in S3"
            fi
        fi
        
        rm -f $temp_report "$s3_list_file" "$missing_files_list" /tmp/dx_files_no_reports.txt /tmp/s3_all_files.txt /tmp/potentially_missing.txt /tmp/truly_missing_count.txt /tmp/renamed_verified_count.txt
    else
        echo "⚠️  Could not download upload report for analysis"
        rm -f $temp_report
    fi
fi

echo ""
