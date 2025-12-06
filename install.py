#!/usr/bin/env python3
"""
Installation script for AWS Deadline Cloud Software Viewer
"""

import subprocess
import sys
from pathlib import Path


def check_python_version():
    """Check if Python version is 3.8 or higher"""
    if sys.version_info < (3, 8):
        print("Error: Python 3.8 or higher is required")
        print(f"Current version: {sys.version}")
        return False
    print(f"Python version: {sys.version.split()[0]}")
    return True


def check_pip():
    """Check if pip is available"""
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            check=True,
            capture_output=True
        )
        print("pip is available")
        return True
    except subprocess.CalledProcessError:
        print("Error: pip is not available")
        return False


def install_requirements():
    """Install required packages"""
    print("\nInstalling required packages...")
    requirements_file = Path(__file__).parent / "requirements.txt"
    
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(requirements_file)],
            check=True
        )
        print("Successfully installed PyQt6 and dependencies")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error installing requirements: {e}")
        return False


def check_deadline_cli():
    """Check if AWS Deadline Cloud CLI is installed"""
    try:
        result = subprocess.run(
            ["deadline", "--version"],
            capture_output=True,
            text=True,
            check=True
        )
        print(f"AWS Deadline Cloud CLI is installed: {result.stdout.strip()}")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("  Warning: AWS Deadline Cloud CLI is not installed")
        print("   Install it with: pip install deadline[gui]")
        print("   Or follow: https://docs.aws.amazon.com/deadline-cloud/latest/developerguide/deadline-cloud-cli.html")
        return False


def check_aws_credentials():
    """Check if AWS credentials are configured"""
    try:
        subprocess.run(
            ["aws", "sts", "get-caller-identity"],
            capture_output=True,
            check=True
        )
        print("AWS credentials are configured")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("  Warning: AWS credentials may not be configured")
        print("   Configure them with: aws configure")
        return False


def main():
    """Main installation process"""
    print("=" * 60)
    print("AWS Deadline Cloud Software Viewer - Installation")
    print("=" * 60)
    print()
    
    # Check Python version
    if not check_python_version():
        sys.exit(1)
    
    # Check pip
    if not check_pip():
        sys.exit(1)
    
    # Install requirements
    if not install_requirements():
        sys.exit(1)
    
    print()
    print("=" * 60)
    print("Checking AWS Deadline Cloud CLI")
    print("=" * 60)
    print()
    
    # Check Deadline CLI
    deadline_ok = check_deadline_cli()
    
    # Check AWS credentials
    aws_ok = check_aws_credentials()
    
    print()
    print("=" * 60)
    print("Installation Summary")
    print("=" * 60)
    print()
    
    if deadline_ok and aws_ok:
        print(" All requirements are met!")
        print()
        print("To run the application:")
        print(f"  python {Path(__file__).parent / 'deadline_software_viewer.py'}")
        print()
        print("Or on Windows:")
        print(f"  python deadline_software_viewer.py")
    else:
        print("  Installation completed with warnings")
        print()
        if not deadline_ok:
            print("  Install AWS Deadline Cloud CLI:")
            print("   pip install deadline[gui]")
            print()
        if not aws_ok:
            print("  Configure AWS credentials:")
            print("   aws configure")
            print()
        print("After addressing the warnings, you can run:")
        print(f"  python {Path(__file__).parent / 'deadline_software_viewer.py'}")
    
    print()


if __name__ == "__main__":
    main()
