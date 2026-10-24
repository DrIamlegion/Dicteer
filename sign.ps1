# Sign one or more files with an Authenticode code-signing certificate.
#
#   .\sign.ps1 -Path "dist\Dicteer\Dicteer.exe" -PfxPath C:\certs\freebuff.pfx
#   .\sign.ps1 -Path dist\setup.exe -CertThumbprint 1A2B3C...
#
# The fingerprint of the certificate and the result of the signature check are
# printed for each file. A signature made with a certificate Windows does not
# trust yet reports "UnknownError"/"NotTrusted"; that is a trust problem, not a
# signing failure.
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string[]]$Path,

    [string]$PfxPath,

    # Prefer the FREEBUFF_SIGN_PFX_PASSWORD environment variable: a password on
    # the command line is visible to other processes.
    [string]$PfxPassword,

    [Alias('CertThumbprint')]
    [string]$Thumbprint,

    [string]$TimestampServer = 'http://timestamp.digicert.com'
)

$ErrorActionPreference = 'Stop'

# The comment above promises the environment variable, so honour it here too;
# build-installer.ps1 passes -PfxPassword explicitly, but this script is also
# run on its own.
if (-not $PfxPassword) { $PfxPassword = $env:FREEBUFF_SIGN_PFX_PASSWORD }

function Get-SigningCertificate {
    if ($PfxPath) {
        if (-not (Test-Path -LiteralPath $PfxPath)) {
            throw "Certificate file not found: $PfxPath"
        }
        $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
        try {
            $resolved = (Resolve-Path -LiteralPath $PfxPath).Path
            $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
                $resolved, $PfxPassword, $flags)
        } catch {
            throw "Could not open $PfxPath (wrong password?): $($_.Exception.Message)"
        }
        if (-not $certificate.HasPrivateKey) {
            throw "$PfxPath does not contain a private key"
        }
        return $certificate
    }

    if ($Thumbprint) {
        $clean = ($Thumbprint -replace '[^0-9A-Fa-f]', '').ToUpperInvariant()
        foreach ($store in @('Cert:\CurrentUser\My', 'Cert:\LocalMachine\My')) {
            $match = Get-ChildItem -Path $store -ErrorAction SilentlyContinue |
                Where-Object { $_.Thumbprint -eq $clean -and $_.HasPrivateKey } |
                Select-Object -First 1
            if ($match) { return $match }
        }
        throw "No certificate with a private key and thumbprint $Thumbprint in CurrentUser\My or LocalMachine\My"
    }

    throw 'Pass -PfxPath or -CertThumbprint (or set FREEBUFF_SIGN_PFX / FREEBUFF_SIGN_THUMBPRINT).'
}

$certificate = Get-SigningCertificate
Write-Host "Certificate: $($certificate.Subject) [$($certificate.Thumbprint)]" -ForegroundColor Cyan

$codeSigningEku = '1.3.6.1.5.5.7.3.3'
$hasEku = $certificate.Extensions |
    Where-Object { $_.Oid.Value -eq '2.5.29.37' } |
    ForEach-Object { $_.Format($false) } |
    Where-Object { $_ -match [regex]::Escape($codeSigningEku) }
if (-not $hasEku) {
    Write-Warning 'This certificate does not list the Code Signing EKU; Windows may reject the signature.'
}

$results = foreach ($file in $Path) {
    if (-not (Test-Path -LiteralPath $file)) { throw "File not found: $file" }
    Write-Host "Signing $file" -ForegroundColor Cyan
    $result = Set-AuthenticodeSignature -FilePath $file -Certificate $certificate `
        -TimestampServer $TimestampServer -HashAlgorithm SHA256

    if (-not $result.SignerCertificate) {
        throw "Signing failed for ${file}: $($result.StatusMessage)"
    }

    $check = Get-AuthenticodeSignature -FilePath $file
    $stamped = $null -ne $check.TimeStamperCertificate
    Write-Host ("  {0}: {1} (timestamp: {2})" -f (Split-Path $file -Leaf), $check.Status, $(if ($stamped) { 'yes' } else { 'no' }))

    [pscustomobject]@{
        File       = (Resolve-Path -LiteralPath $file).Path
        Thumbprint = $check.SignerCertificate.Thumbprint
        Status     = $check.Status.ToString()
        Timestamp  = $stamped
    }
}

if (-not $results) { throw 'Nothing was signed' }
$results
