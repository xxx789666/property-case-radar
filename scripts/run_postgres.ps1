$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$pgCtl = "D:\PostgreSQL\17\bin\pg_ctl.exe"
$data = "D:\PostgreSQL\17\data"
$port = "15432"

foreach ($requiredPath in @($pgCtl, $data)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required PostgreSQL path is missing: $requiredPath"
    }
}

$logDirectory = Join-Path $repositoryRoot "logs"
$logPath = Join-Path $logDirectory "postgres-local.log"
$stdoutLog = Join-Path $logDirectory "postgres-self-heal-ctl.out.log"
$stderrLog = Join-Path $logDirectory "postgres-self-heal-ctl.err.log"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

& $pgCtl status -D $data 2>$null
if ($LASTEXITCODE -eq 0) {
    return
}

foreach ($controlFile in @($stdoutLog, $stderrLog)) {
    if (Test-Path -LiteralPath $controlFile) {
        Remove-Item -LiteralPath $controlFile -Force
    }
}

$argumentList = @(
    "start",
    "-D", ('"{0}"' -f ($data -replace '"', '\"')),
    "-l", ('"{0}"' -f ($logPath -replace '"', '\"')),
    "-o", ('"-p {0}"' -f $port),
    "-w",
    "-t", "60"
)
$quotedApp = '"{0}"' -f ($pgCtl -replace '"', '\"')
$commandLine = $quotedApp + " " + ($argumentList -join " ")

if (-not ("DetachedWin32Process" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public class DetachedLaunchResult {
    public bool TimedOut;
    public uint ExitCode;
    public string Error;
}

public static class DetachedWin32Process {
    public const uint CREATE_NO_WINDOW = 0x08000000;
    public const uint CREATE_BREAKAWAY_FROM_JOB = 0x01000000;
    public const uint CREATE_NEW_PROCESS_GROUP = 0x00000200;
    public const uint EXTENDED_STARTUPINFO_PRESENT = 0x00080000;
    public const uint STARTF_USESTDHANDLES = 0x00000100;
    public const uint GENERIC_WRITE = 0x40000000;
    public const uint GENERIC_READ = 0x80000000;
    public const uint FILE_SHARE_READ = 0x00000001;
    public const uint FILE_SHARE_WRITE = 0x00000002;
    public const uint CREATE_ALWAYS = 2;
    public const uint OPEN_EXISTING = 3;
    public const uint FILE_ATTRIBUTE_NORMAL = 0x80;
    public const uint WAIT_OBJECT_0 = 0;
    public const uint WAIT_TIMEOUT = 0x102;
    public const int PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002;

    [StructLayout(LayoutKind.Sequential)]
    public struct SECURITY_ATTRIBUTES {
        public int nLength;
        public IntPtr lpSecurityDescriptor;
        public int bInheritHandle;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct STARTUPINFO {
        public int cb;
        public IntPtr lpReserved;
        public IntPtr lpDesktop;
        public IntPtr lpTitle;
        public int dwX;
        public int dwY;
        public int dwXSize;
        public int dwYSize;
        public int dwXCountChars;
        public int dwYCountChars;
        public int dwFillAttribute;
        public int dwFlags;
        public short wShowWindow;
        public short cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput;
        public IntPtr hStdOutput;
        public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct STARTUPINFOEX {
        public STARTUPINFO StartupInfo;
        public IntPtr lpAttributeList;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PROCESS_INFORMATION {
        public IntPtr hProcess;
        public IntPtr hThread;
        public int dwProcessId;
        public int dwThreadId;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool InitializeProcThreadAttributeList(
        IntPtr lpAttributeList, int dwAttributeCount, int dwFlags, ref IntPtr lpSize);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool UpdateProcThreadAttribute(
        IntPtr lpAttributeList, uint dwFlags, IntPtr Attribute, IntPtr lpValue,
        IntPtr cbSize, IntPtr lpPreviousValue, IntPtr lpReturnSize);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern void DeleteProcThreadAttributeList(IntPtr lpAttributeList);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CreateProcess(
        string lpApplicationName, string lpCommandLine, IntPtr lpProcessAttributes,
        IntPtr lpThreadAttributes, bool bInheritHandles, uint dwCreationFlags,
        IntPtr lpEnvironment, string lpCurrentDirectory,
        ref STARTUPINFOEX lpStartupInfo, out PROCESS_INFORMATION lpProcessInformation);

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateFile(
        string lpFileName, uint dwDesiredAccess, uint dwShareMode,
        ref SECURITY_ATTRIBUTES lpSecurityAttributes, uint dwCreationDisposition,
        uint dwFlagsAndAttributes, IntPtr hTemplateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr hObject);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);

    static IntPtr OpenInheritableFile(string path, uint access, uint disposition) {
        SECURITY_ATTRIBUTES sa = new SECURITY_ATTRIBUTES();
        sa.nLength = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
        sa.bInheritHandle = 1;
        IntPtr handle = CreateFile(
            path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, ref sa,
            disposition, FILE_ATTRIBUTE_NORMAL, IntPtr.Zero);
        if (handle == new IntPtr(-1)) {
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        }
        return handle;
    }

    public static DetachedLaunchResult StartAndWait(
        string application, string commandLine, string stdoutPath, string stderrPath,
        int timeoutMs) {
        DetachedLaunchResult result = new DetachedLaunchResult();
        IntPtr stdoutHandle = IntPtr.Zero;
        IntPtr stderrHandle = IntPtr.Zero;
        IntPtr stdinHandle = IntPtr.Zero;
        IntPtr attrList = IntPtr.Zero;
        IntPtr handleBuf = IntPtr.Zero;
        PROCESS_INFORMATION pi = new PROCESS_INFORMATION();
        bool started = false;
        try {
            stdoutHandle = OpenInheritableFile(stdoutPath, GENERIC_WRITE, CREATE_ALWAYS);
            stderrHandle = OpenInheritableFile(stderrPath, GENERIC_WRITE, CREATE_ALWAYS);
            stdinHandle = OpenInheritableFile(@"\\.\NUL", GENERIC_READ, OPEN_EXISTING);

            IntPtr size = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
            attrList = Marshal.AllocHGlobal((int)size);
            if (!InitializeProcThreadAttributeList(attrList, 1, 0, ref size)) {
                result.Error = "InitializeProcThreadAttributeList failed: " + Marshal.GetLastWin32Error();
                return result;
            }
            handleBuf = Marshal.AllocHGlobal(IntPtr.Size * 3);
            Marshal.WriteIntPtr(handleBuf, 0, stdinHandle);
            Marshal.WriteIntPtr(handleBuf, IntPtr.Size, stdoutHandle);
            Marshal.WriteIntPtr(handleBuf, IntPtr.Size * 2, stderrHandle);
            if (!UpdateProcThreadAttribute(
                    attrList, 0, (IntPtr)PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handleBuf,
                    (IntPtr)(IntPtr.Size * 3), IntPtr.Zero, IntPtr.Zero)) {
                result.Error = "UpdateProcThreadAttribute failed: " + Marshal.GetLastWin32Error();
                return result;
            }

            STARTUPINFOEX si = new STARTUPINFOEX();
            si.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
            si.StartupInfo.dwFlags = unchecked((int)STARTF_USESTDHANDLES);
            si.StartupInfo.hStdInput = stdinHandle;
            si.StartupInfo.hStdOutput = stdoutHandle;
            si.StartupInfo.hStdError = stderrHandle;
            si.lpAttributeList = attrList;

            uint flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP |
                CREATE_BREAKAWAY_FROM_JOB | EXTENDED_STARTUPINFO_PRESENT;
            started = CreateProcess(
                application, commandLine, IntPtr.Zero, IntPtr.Zero, true, flags,
                IntPtr.Zero, null, ref si, out pi);
            if (!started) {
                int firstError = Marshal.GetLastWin32Error();
                flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | EXTENDED_STARTUPINFO_PRESENT;
                started = CreateProcess(
                    application, commandLine, IntPtr.Zero, IntPtr.Zero, true, flags,
                    IntPtr.Zero, null, ref si, out pi);
                if (!started) {
                    result.Error = "CreateProcess failed: " + firstError + "/" + Marshal.GetLastWin32Error();
                    return result;
                }
            }

            uint wait = WaitForSingleObject(pi.hProcess, (uint)timeoutMs);
            if (wait == WAIT_TIMEOUT) {
                TerminateProcess(pi.hProcess, 1);
                result.TimedOut = true;
                return result;
            }
            uint exitCode;
            if (!GetExitCodeProcess(pi.hProcess, out exitCode)) {
                result.Error = "GetExitCodeProcess failed: " + Marshal.GetLastWin32Error();
                return result;
            }
            result.ExitCode = exitCode;
            return result;
        } finally {
            if (pi.hThread != IntPtr.Zero) {
                CloseHandle(pi.hThread);
            }
            if (pi.hProcess != IntPtr.Zero) {
                CloseHandle(pi.hProcess);
            }
            if (attrList != IntPtr.Zero) {
                DeleteProcThreadAttributeList(attrList);
                Marshal.FreeHGlobal(attrList);
            }
            if (handleBuf != IntPtr.Zero) {
                Marshal.FreeHGlobal(handleBuf);
            }
            if (stdinHandle != IntPtr.Zero && stdinHandle != new IntPtr(-1)) {
                CloseHandle(stdinHandle);
            }
            if (stdoutHandle != IntPtr.Zero && stdoutHandle != new IntPtr(-1)) {
                CloseHandle(stdoutHandle);
            }
            if (stderrHandle != IntPtr.Zero && stderrHandle != new IntPtr(-1)) {
                CloseHandle(stderrHandle);
            }
        }
    }
}
'@
}

$launch = [DetachedWin32Process]::StartAndWait(
    $pgCtl,
    $commandLine,
    $stdoutLog,
    $stderrLog,
    70000
)
if ($launch.Error) {
    throw $launch.Error
}
if ($launch.TimedOut) {
    throw "PostgreSQL startup timed out waiting for pg_ctl"
}
$exitCode = [int]$launch.ExitCode
if ($exitCode -eq 0) {
    return
}

$stderrDetail = ""
if (Test-Path -LiteralPath $stderrLog) {
    $stderrDetail = (
        Get-Content -LiteralPath $stderrLog -ErrorAction SilentlyContinue |
            Out-String
    ).Trim()
}
$stdoutDetail = ""
if (Test-Path -LiteralPath $stdoutLog) {
    $stdoutDetail = (
        Get-Content -LiteralPath $stdoutLog -ErrorAction SilentlyContinue |
            Out-String
    ).Trim()
}
$controlDetail = $stderrDetail
if ($stdoutDetail) {
    if ($controlDetail) {
        $controlDetail = $controlDetail + [Environment]::NewLine + $stdoutDetail
    } else {
        $controlDetail = $stdoutDetail
    }
}
if (-not $controlDetail) {
    $controlDetail = "no control log output"
}
throw "PostgreSQL startup failed: $exitCode; $controlDetail"
