// scan.cjs — dump every code file in the project into one text file
// run: node scan.cjs

const fs = require('fs');
const path = require('path');

const OUTPUT_FILE = 'all_code.txt';

// dirs to skip entirely
const IGNORED_DIRS = new Set([
    '.git', '.vscode', '.idea', 'node_modules',
    'dist', 'build', 'coverage', '__pycache__',
]);

// specific files to skip
const IGNORED_FILES = new Set([
    OUTPUT_FILE,                       // would create an infinite loop
    'package-lock.json', 'yarn.lock',
    '.DS_Store', 'Thumbs.db',
]);

// skip these extensions — they're binary
const BINARY_EXTENSIONS = new Set([
    '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg', '.webp',
    '.mp3', '.mp4', '.wav', '.ogg',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.zip', '.tar', '.gz', '.7z', '.rar',
    '.exe', '.dll', '.bin', '.dat', '.db', '.sqlite',
    '.traineddata',
]);

function isCodeFile(filename) {
    const ext = path.extname(filename).toLowerCase();
    if (IGNORED_FILES.has(filename)) return false;
    if (BINARY_EXTENSIONS.has(ext)) return false;
    return true;
}

// "density & threshold" garble detector: any chunk where the non-letter/number/
// punctuation/symbol chars exceed either 1000 in absolute count OR 30% of the
// stripped text is probably binary/key-dump, not source
function isGarbled(content) {
    const stripped = content.replace(/\s+/g, '');
    const weird = stripped.match(/[^\p{L}\p{N}\p{P}\p{S}]/gu);
    if (!weird) return false;
    if (weird.length > 1000) return true;
    if (stripped.length > 0 && weird.length / stripped.length > 0.3) return true;
    return false;
}

function scanDirectory(dir, fileList = []) {
    for (const file of fs.readdirSync(dir)) {
        const filePath = path.join(dir, file);
        const stat = fs.statSync(filePath);
        if (stat.isDirectory()) {
            if (!IGNORED_DIRS.has(file)) scanDirectory(filePath, fileList);
        } else if (isCodeFile(file)) {
            fileList.push(filePath);
        }
    }
    return fileList;
}

function generateCodeDump() {
    console.log('Scanning directory...');
    const allFiles = scanDirectory(__dirname);
    const writeStream = fs.createWriteStream(path.join(__dirname, OUTPUT_FILE));
    let count = 0;

    for (const filePath of allFiles) {
        // normalize slashes so output looks the same on win/mac/linux
        const normalizedPath = path.relative(__dirname, filePath).split(path.sep).join('/');

        try {
            let content = fs.readFileSync(filePath, 'utf8');
            if (isGarbled(content)) {
                console.warn(`[!] Garbled/Binary: ${normalizedPath}`);
                content = 'unreadable';
            }
            writeStream.write(`\n--- START OF FILE ${normalizedPath} ---\n`);
            writeStream.write(content);
            writeStream.write('\n\n');
            console.log(`Included: ${normalizedPath}`);
            count++;
        } catch (err) {
            console.error(`Error reading ${normalizedPath}: ${err.message}`);
        }
    }

    writeStream.end();
    console.log(`\nScanned ${count} files → ${OUTPUT_FILE}`);
}

generateCodeDump();