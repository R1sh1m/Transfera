const https = require('https');
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const PYTHON_VERSION = '3.12.8';
const DOWNLOAD_URL = `https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-embed-amd64.zip`;
const ZIP_PATH = path.resolve(__dirname, '../python-embed.zip');
const PYTHON_DIR = path.resolve(__dirname, '../python-bin');

function downloadFile(url, dest) {
  return new Promise((resolve, reject) => {
    console.log(`[portable-python] Downloading ${url} -> ${dest}...`);
    const file = fs.createWriteStream(dest);
    const request = (targetUrl) => {
      https.get(targetUrl, (response) => {
        if (response.statusCode === 301 || response.statusCode === 302) {
          request(response.headers.location);
          return;
        }
        if (response.statusCode !== 200) {
          reject(new Error(`Failed to download: ${response.statusCode}`));
          return;
        }
        response.pipe(file);
        file.on('finish', () => {
          file.close(resolve);
        });
      }).on('error', (err) => {
        fs.unlink(dest, () => reject(err));
      });
    };
    request(url);
  });
}

function extractZip(zipPath, destDir) {
  console.log(`[portable-python] Extracting ${zipPath} to ${destDir}...`);
  if (fs.existsSync(destDir)) {
    fs.rmSync(destDir, { recursive: true, force: true });
  }
  fs.mkdirSync(destDir, { recursive: true });
  
  // On Windows, use PowerShell's Expand-Archive cmdlet (safe single-quote escaping)
  const escapedZip = zipPath.replace(/'/g, "''");
  const escapedDest = destDir.replace(/'/g, "''");
  const cmd = `powershell -Command "Expand-Archive -Path '${escapedZip}' -DestinationPath '${escapedDest}' -Force"`;
  execSync(cmd, { stdio: 'inherit' });
}

function configurePathFile(destDir) {
  console.log(`[portable-python] Reconfiguring ._pth file in ${destDir}...`);
  const files = fs.readdirSync(destDir);
  const pthFile = files.find(f => f.endsWith('._pth'));
  if (!pthFile) {
    throw new Error('Could not find ._pth file in extracted Python directory');
  }
  const pthPath = path.join(destDir, pthFile);
  const zipLib = pthFile.replace('._pth', '.zip');
  
  // Enable site-packages folder and import site module
  const content = `${zipLib}\n.\nsite-packages\n\n# Uncomment to run site.main() automatically\nimport site\n`;
  fs.writeFileSync(pthPath, content);
}

function installRequirements(destDir) {
  const sitePackagesDir = path.join(destDir, 'site-packages');
  fs.mkdirSync(sitePackagesDir, { recursive: true });
  
  const requirementsPath = path.resolve(__dirname, '../../backend/requirements.txt');
  
  // Locate local dev virtual env python for reliable python 3.12 + pip execution
  const venvPython = path.resolve(__dirname, '../../.venv/Scripts/python.exe');
  let pythonCmd = 'python';
  if (fs.existsSync(venvPython)) {
    pythonCmd = venvPython;
  } else {
    try {
      execSync('py -3.12 --version', { stdio: 'ignore' });
      pythonCmd = 'py -3.12';
    } catch (e) {
      // Fallback to system python
    }
  }
  
  console.log(`[portable-python] Using python interpreter: ${pythonCmd}`);
  
  // Run pip install targeting the site-packages directory of the portable python environment.
  // Use --upgrade to ensure requirements and dependencies are written directly into target dir.
  const cmd = `"${pythonCmd}" -m pip install --upgrade -r "${requirementsPath}" --target "${sitePackagesDir}"`;
  console.log(`[portable-python] Running pip command: ${cmd}`);
  execSync(cmd, { stdio: 'inherit' });
}

function cleanUnnecessaryBinaries(sitePackagesDir) {
  console.log(`[portable-python] Scanning and cleaning unnecessary binaries from ${sitePackagesDir}...`);
  if (!fs.existsSync(sitePackagesDir)) return;

  function scanAndClean(dir) {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        scanAndClean(fullPath);
      } else if (entry.isFile()) {
        const ext = path.extname(entry.name).toLowerCase();
        // Remove executable scripts, test executables or debug binaries that trigger AV
        if (ext === '.exe' || ext === '.bat' || ext === '.cmd' || entry.name.toLowerCase() === 'test_suite') {
          console.log(`[portable-python] Removing AV-trigger risk file: ${fullPath}`);
          try {
            fs.unlinkSync(fullPath);
          } catch (err) {
            console.warn(`[portable-python] Failed to remove ${fullPath}: ${err.message}`);
          }
        }
      }
    }
  }

  scanAndClean(sitePackagesDir);
}

async function main() {
  if (process.platform !== 'win32') {
    console.log('[portable-python] Skipping portable Python build: platform is not Windows.');
    // Ensure the folder exists so electron-builder doesn't fail on non-Windows dev platforms
    if (!fs.existsSync(PYTHON_DIR)) {
      fs.mkdirSync(PYTHON_DIR, { recursive: true });
    }
    return;
  }

  try {
    // Only download and build if python-bin doesn't exist or is empty
    const pythonExe = path.join(PYTHON_DIR, 'python.exe');
    const sitePackages = path.join(PYTHON_DIR, 'site-packages');
    if (fs.existsSync(pythonExe) && fs.existsSync(sitePackages)) {
      console.log('[portable-python] Portable Python environment already exists in python-bin. Skipping rebuild.');
      return;
    }

    await downloadFile(DOWNLOAD_URL, ZIP_PATH);
    extractZip(ZIP_PATH, PYTHON_DIR);
    configurePathFile(PYTHON_DIR);
    installRequirements(PYTHON_DIR);
    cleanUnnecessaryBinaries(sitePackages);
    
    // Clean up zip
    if (fs.existsSync(ZIP_PATH)) {
      fs.unlinkSync(ZIP_PATH);
    }
    console.log('[portable-python] Portable Python environment built successfully!');
  } catch (error) {
    console.error('[portable-python] Failed to build portable Python environment:', error);
    process.exit(1);
  }
}

main();
