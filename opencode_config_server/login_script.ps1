# Login command for Windows, run by the OpenCode harness through the manifest
# served at <server>/windows/.well-known/opencode. Windows PowerShell 5.1, the one
# every Windows 10/11 machine has; also runs on PowerShell 7.
#
# Same contract and behaviour as login_script.sh:
#   * stdin is closed; stdout (trimmed) becomes the stored credential, so only
#     the token goes there and everything else goes to stderr
#   * exit != 0 fails the login and shows stderr
#   * credentials: OPENCODE_CORP_TOKEN, or OPENCODE_CORP_USER + _PASSWORD, or
#     the credentials file, or else a browser login (OPENCODE_CORP_NO_BROWSER=1
#     prints the address without opening it)
#   * then the corporate installer (bootstrap/install.ps1), and the variables:
#     the employee's own values always win over the company's
#
# Keep this file ASCII: the manifest pins its SHA-256 and the launcher hashes
# the downloaded text as UTF-8.
param([string]$Server)

$ErrorActionPreference = 'Stop'
$Server = "$Server".TrimEnd('/')
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Net.Http

$script:Token = $null

function Say([string]$Message) { [Console]::Error.WriteLine("opencode-corp: $Message") }
function Fail([int]$Code, [string]$Message) { Say $Message; exit $Code }

if (-not $Server) { Fail 2 'no server URL was passed to the login command' }

$Http = New-Object System.Net.Http.HttpClient
$Http.Timeout = [TimeSpan]::FromSeconds(30)

# Never throws: Status 0 means no answer at all (network, VPN, TLS).
function Invoke-Corp([string]$Method, [string]$Path, [hashtable]$Headers = @{}, [string]$Body = $null) {
  $request = New-Object System.Net.Http.HttpRequestMessage ([System.Net.Http.HttpMethod]::new($Method)), ($Server + $Path)
  [void]$request.Headers.TryAddWithoutValidation('Accept', 'application/json')
  foreach ($key in $Headers.Keys) { [void]$request.Headers.TryAddWithoutValidation($key, $Headers[$key]) }
  if ($null -ne $Body) {
    $request.Content = New-Object System.Net.Http.StringContent ($Body, [Text.Encoding]::UTF8, 'application/json')
  }
  try {
    $response = $Http.SendAsync($request).GetAwaiter().GetResult()
    $text = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    return @{ Status = [int]$response.StatusCode; Body = $text }
  } catch {
    return @{ Status = 0; Body = '' }
  }
}

function Read-Json([string]$Text) {
  try { return ($Text | ConvertFrom-Json) } catch { return $null }
}

function Get-Detail($Response) {
  $data = Read-Json $Response.Body
  if ($data -and $data.PSObject.Properties['detail'] -and $data.detail -is [string]) { return ": $($data.detail)" }
  return ''
}

function Get-ConfigHome {
  if ($env:XDG_CONFIG_HOME) { return $env:XDG_CONFIG_HOME }
  return (Join-Path $HOME '.config')
}

# -- credentials ---------------------------------------------------------------

function Read-Credentials {
  $credentials = @{ Token = $env:OPENCODE_CORP_TOKEN; User = $env:OPENCODE_CORP_USER; Password = $env:OPENCODE_CORP_PASSWORD }
  $file = $env:OPENCODE_CORP_CREDENTIALS
  if (-not $file) { $file = Join-Path (Get-ConfigHome) 'opencode-corp\credentials' }
  if (-not $credentials.Token -and -not $credentials.User -and (Test-Path -LiteralPath $file)) {
    foreach ($line in Get-Content -LiteralPath $file) {
      $line = ($line -split '#', 2)[0]
      if ($line -notmatch '=') { continue }
      $key, $value = $line -split '=', 2
      $key = $key.Trim()
      $value = $value.Trim().Trim('"')
      switch ($key) {
        { $_ -in 'token', 'api_key' } { $credentials.Token = $value }
        { $_ -in 'user', 'username' } { $credentials.User = $value }
        'password' { $credentials.Password = $value }
      }
    }
  }
  return $credentials
}

function Complete-Login($Response) {
  if ($Response.Status -eq 0) { Fail 5 "cannot reach $Server (network, VPN or TLS problem)" }
  if ($Response.Status -ne 200) { Fail 6 "login rejected by $Server (HTTP $($Response.Status))$(Get-Detail $Response)" }
  $data = Read-Json $Response.Body
  if (-not $data -or -not $data.token) { Fail 7 'the server answered without a token' }
  $script:Token = [string]$data.token
}

function Invoke-BrowserLogin {
  $start = Invoke-Corp 'POST' '/auth/device'
  if ($start.Status -eq 0) { Fail 5 "cannot reach $Server (network, VPN or TLS problem)" }
  if ($start.Status -ne 200) { Fail 6 "the server refused to start a browser login (HTTP $($start.Status))$(Get-Detail $start)" }
  $grant = Read-Json $start.Body
  if (-not $grant -or -not $grant.device_code -or -not $grant.verification_uri_complete) { Fail 7 'the server answered without a login code' }
  $interval = 2
  if ($grant.interval) { $interval = [int]$grant.interval }
  $expires = 300
  if ($grant.expires_in) { $expires = [int]$grant.expires_in }

  Say 'sign in from your browser to finish the login:'
  Say "    $($grant.verification_uri_complete)"
  Say "    code: $($grant.user_code)"
  if ($env:OPENCODE_CORP_NO_BROWSER -ne '1') {
    try {
      Start-Process $grant.verification_uri_complete | Out-Null
      Say 'a browser window should open; if it does not, open the address above'
    } catch { }
  }

  $deadline = (Get-Date).AddSeconds($expires)
  $body = '{"device_code":"' + $grant.device_code + '"}'
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds $interval
    $poll = Invoke-Corp 'POST' '/auth/device/token' @{} $body
    if ($poll.Status -eq 200) { Complete-Login $poll; return }
    if ($poll.Status -eq 0) { continue }   # a network blip while waiting is not fatal
    if ($poll.Status -eq 400) {
      $failure = Read-Json $poll.Body
      $code = ''
      if ($failure) { $code = $failure.error }
      if ($code -eq 'authorization_pending') { continue }
      if ($code -eq 'slow_down') { $interval += 2; continue }
      Fail 8 "browser login failed$(Get-Detail $poll)"
    }
    Fail 6 "browser login rejected by $Server (HTTP $($poll.Status))$(Get-Detail $poll)"
  }
  Fail 8 'the login code expired before it was approved; run the login again'
}

# -- variables: the employee's own values always win -----------------------------
# See login_script.sh for the rules; this is the same logic. "Their own" means
# present in the environment this login runs with (the OpenCode server's, so
# what {env:NAME} sees) or in the service configuration, with a value this
# script did not put there itself (env.managed keeps a digest per name).

$StateDir = Join-Path (Get-ConfigHome) 'opencode-corp'
$Managed = Join-Path $StateDir 'env.managed'
$HostExe = (Get-Process -Id $PID).Path

function Find-OpenCode {
  if ($env:OPENCODE_CORP_OPENCODE_BIN) { return $env:OPENCODE_CORP_OPENCODE_BIN }
  try {
    $parentId = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
    $parent = (Get-Process -Id $parentId).Path
    if ($parent -and ((Split-Path $parent -Leaf) -like 'opencode*')) { return $parent }
  } catch { }
  $command = Get-Command opencode -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  return ''
}

function Get-Digest([string]$Value) {
  $sha = [Security.Cryptography.SHA256]::Create()
  $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))
  return ([BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant()
}

function Get-Ours([string]$Name) {
  if (-not (Test-Path -LiteralPath $Managed)) { return '' }
  foreach ($line in Get-Content -LiteralPath $Managed) {
    $parts = $line -split ' ', 2
    if ($parts[0] -eq $Name -and $parts.Count -eq 2) { return $parts[1] }
  }
  return ''
}

function Get-ServiceValue([string]$Name) {
  if (-not $script:OpenCodeBin) { return '' }
  try {
    $output = & $script:OpenCodeBin service get env $Name 2>$null
    if ($null -eq $output) { return '' }
    return (($output | ForEach-Object { "$_" }) -join "`n").TrimEnd()
  } catch { return '' }
}

function Test-EmployeeOwns([string]$Name) {
  $ours = Get-Ours $Name
  foreach ($value in @([Environment]::GetEnvironmentVariable($Name), (Get-ServiceValue $Name))) {
    if ([string]::IsNullOrEmpty($value)) { continue }
    if (-not $ours -or (Get-Digest $value) -ne $ours) { return $true }
  }
  return $false
}

function Send-OwnEnv {
  $auth = @{ Authorization = "Bearer $script:Token" }
  $listing = Invoke-Corp 'GET' '/bootstrap/names' $auth
  if ($listing.Status -ne 200) { return }
  $own = @()
  foreach ($name in ($listing.Body -split "`n")) {
    $name = $name.Trim()
    if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
    if (Test-EmployeeOwns $name) { $own += $name }
  }
  $json = '{"names":[' + (($own | ForEach-Object { '"' + $_ + '"' }) -join ',') + ']}'
  [void](Invoke-Corp 'POST' '/bootstrap/own-env' $auth $json)
  foreach ($name in $own) { Say "${name}: you already define it, so your own value is used instead of the company's" }
}

# Skills: OpenCode loads remote skill sources after the employee's own skills
# folder, so a corporate skill would replace one of theirs with the same name.
# Report which corporate skills they have their own of (names only).
function Send-OwnSkills {
  # Every published name, not our catalogue: that one already leaves out what we
  # reported last time, and comparing against it would hand those back.
  $index = Invoke-Corp 'GET' '/bootstrap/skill-names' @{ Authorization = "Bearer $script:Token" }
  if ($index.Status -ne 200) { return }
  $published = @{}
  foreach ($line in ($index.Body -split "`n")) { if ($line.Trim()) { $published[$line.Trim()] = $true } }
  $base = Join-Path (Get-ConfigHome) 'opencode'
  $names = @{}
  foreach ($dir in @((Join-Path $base 'skill'), (Join-Path $base 'skills'))) {
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { continue }
    $root = (Resolve-Path -LiteralPath $dir).Path
    foreach ($file in Get-ChildItem -LiteralPath $root -Recurse -Depth 6 -Filter 'SKILL.md' -File -ErrorAction SilentlyContinue) {
      $relative = $file.FullName.Substring($root.Length)
      if ($relative -match '[\\/]\.') { continue }   # hidden folders, as OpenCode skips them
      $names[$file.Directory.Name] = $true
    }
    foreach ($file in Get-ChildItem -LiteralPath $root -Filter '*.md' -File -ErrorAction SilentlyContinue) {
      if ($file.Name -ne 'SKILL.md') { $names[[IO.Path]::GetFileNameWithoutExtension($file.Name)] = $true }
    }
  }
  $own = @($names.Keys | Where-Object { $published.ContainsKey($_) -and $_ -match '^[A-Za-z0-9._-]+$' } | Sort-Object)
  $json = '{"names":[' + (($own | ForEach-Object { '"' + $_ + '"' }) -join ',') + ']}'
  [void](Invoke-Corp 'POST' '/bootstrap/own-skills' @{ Authorization = "Bearer $script:Token" } $json)
  foreach ($name in $own) { Say "skill ${name}: you have your own, so the company's version is not used" }
}

function Sync-MachineEnv {
  $listing = Invoke-Corp 'GET' '/bootstrap/env' @{ Authorization = "Bearer $script:Token" }
  if ($listing.Status -ne 200) { return }
  $lines = @($listing.Body -split "`n" | Where-Object { $_.Trim() })
  if ($lines.Count -eq 0 -and -not ((Test-Path -LiteralPath $Managed) -and (Get-Item -LiteralPath $Managed).Length -gt 0)) { return }
  if (-not $script:OpenCodeBin) {
    Say 'cannot find the opencode binary, so the machine environment was not updated'
    return
  }
  $plan = New-Object System.Collections.Generic.List[string]
  $next = New-Object System.Collections.Generic.List[string]
  $sent = @{}
  foreach ($line in $lines) {
    $name, $encoded = $line.Trim() -split ' ', 2
    $sent[$name] = $true
    if (Test-EmployeeOwns $name) { continue }   # reported above
    try { $value = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded)) } catch { continue }
    if ((Get-ServiceValue $name) -ne $value) { $plan.Add("set $name $encoded") }
    $next.Add("$name $(Get-Digest $value)")
  }
  if (Test-Path -LiteralPath $Managed) {
    foreach ($line in Get-Content -LiteralPath $Managed) {
      $name, $ours = $line -split ' ', 2
      if (-not $name -or $sent.ContainsKey($name)) { continue }
      $current = Get-ServiceValue $name
      if ($current -and (Get-Digest $current) -eq $ours) { $plan.Add("unset $name") }
    }
  }
  New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
  $utf8 = New-Object Text.UTF8Encoding $false
  if ($plan.Count -eq 0) {
    [IO.File]::WriteAllLines($Managed, [string[]]$next, $utf8)
    return
  }
  $temp = [IO.Path]::GetTempPath()
  $planFile = Join-Path $temp "opencode-corp-plan-$([Guid]::NewGuid()).txt"
  $nextFile = Join-Path $temp "opencode-corp-next-$([Guid]::NewGuid()).txt"
  $jobFile = Join-Path $temp "opencode-corp-env-$([Guid]::NewGuid()).ps1"
  [IO.File]::WriteAllLines($planFile, [string[]]$plan, $utf8)
  [IO.File]::WriteAllLines($nextFile, [string[]]$next, $utf8)
  # `opencode service set env` stops the service this login runs inside, so a
  # separate process applies the plan once the login has returned.
  [IO.File]::WriteAllText($jobFile, @'
param($Bin, $Plan, $Next, $Managed)
$delay = 3
if ($env:OPENCODE_CORP_ENV_DELAY) { $delay = [int]$env:OPENCODE_CORP_ENV_DELAY }
Start-Sleep -Seconds $delay
$running = ("$(& $Bin service status 2>$null | Select-Object -First 1)".Trim() -ne 'stopped')
foreach ($line in Get-Content -LiteralPath $Plan) {
  $action, $name, $encoded = $line -split ' ', 3
  if ($action -eq 'set') {
    & $Bin service set env $name ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))) 2>$null | Out-Null
  } elseif ($action -eq 'unset') {
    & $Bin service unset env $name 2>$null | Out-Null
  }
}
Move-Item -Force -LiteralPath $Next -Destination $Managed
Remove-Item -Force -LiteralPath $Plan, $MyInvocation.MyCommand.Path -ErrorAction SilentlyContinue
if ($running) { & $Bin service start 2>$null | Out-Null }
'@, $utf8)
  $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $jobFile, $script:OpenCodeBin, $planFile, $nextFile, $Managed) |
    ForEach-Object { '"' + $_ + '"' }
  if ($env:OS -eq 'Windows_NT') {
    Start-Process -FilePath $HostExe -ArgumentList $arguments -WindowStyle Hidden | Out-Null
  } else {
    Start-Process -FilePath $HostExe -ArgumentList $arguments | Out-Null
  }
  Say "machine environment: $($plan.Count) change(s) to apply; OpenCode restarts in a few seconds to pick them up"
}

# -- main ------------------------------------------------------------------------
# Everything inside goes to stderr, so nothing but the token can reach stdout.

& {
  $credentials = Read-Credentials
  if ($credentials.Token) {
    Say 'authenticating with the corporate API key'
    Complete-Login (Invoke-Corp 'POST' '/auth/token' @{ 'X-Api-Key' = $credentials.Token })
  } elseif ($credentials.User) {
    if (-not $credentials.Password) { Fail 4 'OPENCODE_CORP_USER is set but no password was provided' }
    Say "authenticating as $($credentials.User)"
    $basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$($credentials.User):$($credentials.Password)"))
    Complete-Login (Invoke-Corp 'POST' '/auth/token' @{ Authorization = "Basic $basic" })
  } else {
    Invoke-BrowserLogin
  }

  if ($env:OPENCODE_CORP_SKIP_BOOTSTRAP -ne '1') {
    $installer = Invoke-Corp 'GET' '/bootstrap/install.ps1' @{ Authorization = "Bearer $script:Token" }
    if ($installer.Status -eq 200 -and $installer.Body) {
      $file = Join-Path ([IO.Path]::GetTempPath()) "opencode-corp-install-$([Guid]::NewGuid()).ps1"
      [IO.File]::WriteAllText($file, $installer.Body, (New-Object Text.UTF8Encoding $false))
      Say 'running the corporate bootstrap'
      & $HostExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $file $Server 2>&1 | ForEach-Object { [Console]::Error.WriteLine("$_") }
      if ($LASTEXITCODE -eq 0) { Say 'bootstrap finished' } else { Say 'bootstrap failed; continuing with the login (re-run it later)' }
      Remove-Item -Force -LiteralPath $file -ErrorAction SilentlyContinue
    }
  }

  try { Send-OwnSkills } catch { }

  if ($env:OPENCODE_CORP_SKIP_ENV -ne '1') {
    $script:OpenCodeBin = Find-OpenCode
    try { Send-OwnEnv } catch { }
    try { Sync-MachineEnv } catch { Say "the machine environment was not updated: $($_.Exception.Message)" }
  }

  Say 'logged in; the corporate configuration loads below your own settings'
} | ForEach-Object { [Console]::Error.WriteLine("$_") }

[Console]::Out.Write($script:Token)
exit 0
