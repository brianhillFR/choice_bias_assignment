$ErrorActionPreference = 'Stop'

Write-Host 'Dropbox OAuth refresh-token helper'
Write-Host 'The app key, secret, authorization code, and refresh token stay in this terminal.'
Write-Host

$appKey = (Read-Host 'Dropbox app key').Trim()
if ([string]::IsNullOrWhiteSpace($appKey)) {
    throw 'The Dropbox app key cannot be empty.'
}

$secureAppSecret = Read-Host 'Dropbox app secret (hidden)' -AsSecureString
$secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureAppSecret)

try {
    $appSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    if ([string]::IsNullOrWhiteSpace($appSecret)) {
        throw 'The Dropbox app secret cannot be empty.'
    }

    $encodedKey = [Uri]::EscapeDataString($appKey)
    $encodedScope = [Uri]::EscapeDataString(
        'files.content.read files.content.write files.metadata.read'
    )
    $authorizationUrl = (
        'https://www.dropbox.com/oauth2/authorize' +
        "?client_id=$encodedKey" +
        '&response_type=code' +
        '&token_access_type=offline' +
        "&scope=$encodedScope"
    )

    Write-Host
    Write-Host 'Open this URL in your browser and authorize the app:'
    Write-Host $authorizationUrl
    Write-Host

    $authorizationCode = (Read-Host 'Paste the authorization code').Trim()
    if ([string]::IsNullOrWhiteSpace($authorizationCode)) {
        throw 'The Dropbox authorization code cannot be empty.'
    }

    $tokenResponse = Invoke-RestMethod `
        -Method Post `
        -Uri 'https://api.dropboxapi.com/oauth2/token' `
        -ContentType 'application/x-www-form-urlencoded' `
        -Body @{
            code = $authorizationCode
            grant_type = 'authorization_code'
            client_id = $appKey
            client_secret = $appSecret
        }

    if ([string]::IsNullOrWhiteSpace($tokenResponse.refresh_token)) {
        throw 'Dropbox did not return a refresh token.'
    }

    Write-Host
    Write-Host 'Copy this into Streamlit Secrets; do not commit it to Git:'
    Write-Host
    Write-Host ('DROPBOX_REFRESH_TOKEN = "' + $tokenResponse.refresh_token + '"')
}
finally {
    if ($secretPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    }
    $appSecret = $null
    $secureAppSecret = $null
}
