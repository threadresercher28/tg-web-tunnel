<?php
ini_set('max_execution_time',300);ini_set('memory_limit','512M');ini_set('zlib.output_compression','Off');ini_set('output_buffering','Off');
if(function_exists('apache_setenv'))apache_setenv('no-gzip','1');
if(session_status()===PHP_SESSION_ACTIVE)session_write_close();
ini_set('display_errors','0');error_reporting(0);
while(ob_get_level())ob_end_clean();

define('MAX_REDIRECTS',5);define('CONNECT_TIMEOUT',15);define('TIMEOUT',60);
define('USER_AGENT','Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36');
define('ALLOWED_HOSTS',['api.telegram.org','telegram.org','t.me','web.telegram.org','cdn.telegram.org','core.telegram.org','upload.telegram.org','venus.telegram.org','aurora.telegram.org','vesta.telegram.org']);
define('ALLOWED_IP_RANGES',['91.108.56.0/22','91.108.4.0/22','91.108.8.0/22','91.108.16.0/22','91.108.12.0/22','149.154.160.0/20','91.105.192.0/23','91.108.20.0/22','185.76.151.0/24','2001:b28:f23d::/48','2001:b28:f23f::/48','2001:67c:4e8::/48','2001:b28:f23c::/48','2a0a:f280::/32']);
define('ALLOWED_CLIENT_HEADERS',['content-type','accept','accept-encoding','accept-language','authorization','user-agent','x-requested-with','cache-control','pragma','referer','origin']);

function ipInRange($ip,$range){$parts=explode('/',$range);if(count($parts)!==2)return false;$subnet=$parts[0];$prefix=(int)$parts[1];
    if(filter_var($ip,FILTER_VALIDATE_IP,FILTER_FLAG_IPV4)){$ipLong=ip2long($ip);$subnetLong=ip2long($subnet);if($ipLong===false||$subnetLong===false)return false;$mask=-1<<(32-$prefix);$subnetLong&=$mask;return($ipLong&$mask)===$subnetLong;}
    if(filter_var($ip,FILTER_VALIDATE_IP,FILTER_FLAG_IPV6)){$ipBin=inet_pton($ip);$subnetBin=inet_pton($subnet);if($ipBin===false||$subnetBin===false)return false;$fullBytes=$prefix>>3;$remainingBits=$prefix&7;
        if($fullBytes>0&&substr($ipBin,0,$fullBytes)!==substr($subnetBin,0,$fullBytes))return false;
        if($remainingBits>0&&$fullBytes<16){$mask=0xFF<<(8-$remainingBits)&0xFF;$ipByte=ord($ipBin[$fullBytes]);$subnetByte=ord($subnetBin[$fullBytes]);if(($ipByte&$mask)!==($subnetByte&$mask))return false;}return true;}return false;}

        function isUrlSafe($url){$parsed=parse_url($url);if(!isset($parsed['host']))return false;$host=strtolower($parsed['host']);
            foreach(ALLOWED_HOSTS as $allowedHost){if($host===$allowedHost||str_ends_with($host,'.'.$allowedHost))return true;}
            if(filter_var($host,FILTER_VALIDATE_IP)){foreach(ALLOWED_IP_RANGES as $range){if(ipInRange($host,$range))return true;}return false;}
            $ips=[];$dns4=@dns_get_record($host,DNS_A);$dns6=@dns_get_record($host,DNS_AAAA);
            if(is_array($dns4)){foreach($dns4 as $rec){if(isset($rec['ip']))$ips[]=$rec['ip'];}}
            if(is_array($dns6)){foreach($dns6 as $rec){if(isset($rec['ipv6']))$ips[]=$rec['ipv6'];}}
            if(empty($ips))return false;
            foreach($ips as $ip){$allowed=false;foreach(ALLOWED_IP_RANGES as $range){if(ipInRange($ip,$range)){$allowed=true;break;}}if(!$allowed)return false;}return true;}

            function resolveAndPinIp($url){$parsed=parse_url($url);$host=$parsed['host']??'';$scheme=$parsed['scheme']??'https';$port=$parsed['port']??($scheme==='https'?443:80);
                if(filter_var($host,FILTER_VALIDATE_IP))return null;$ips=[];$dns4=@dns_get_record($host,DNS_A);$dns6=@dns_get_record($host,DNS_AAAA);
                if(is_array($dns4)){foreach($dns4 as $rec){if(isset($rec['ip']))$ips[]=$rec['ip'];}}
                if(is_array($dns6)){foreach($dns6 as $rec){if(isset($rec['ipv6']))$ips[]=$rec['ipv6'];}}
                if(empty($ips))return null;$chosenIp=$ips[0];foreach($ips as $ip){if(filter_var($ip,FILTER_VALIDATE_IP,FILTER_FLAG_IPV4)){$chosenIp=$ip;break;}}return[$host,$port,$chosenIp];}

                function getClientHeaders(){$rawHeaders=[];
                    if(function_exists('getallheaders')){$rawHeaders=getallheaders();}
                    else{foreach($_SERVER as $name=>$value){if(substr($name,0,5)=='HTTP_'){$headerName=str_replace(' ','-',ucwords(strtolower(str_replace('_',' ',substr($name,5)))));$rawHeaders[$headerName]=$value;}}}
                    $cleanHeaders=[];foreach($rawHeaders as $name=>$value){$safeName=str_replace(["\r","\n"],'',strtolower(trim($name)));$safeValue=str_replace(["\r","\n"],'',trim($value));
                        if(in_array($safeName,ALLOWED_CLIENT_HEADERS,true)){$cleanHeaders[$safeName]=$safeValue;}}return $cleanHeaders;}

                        function normalizeUrl($url){$url=trim($url);if(empty($url))return false;
                            if(!preg_match('~^(?:f|ht)tps?://~i',$url)){$url='http://'.$url;}
                                $parsed=parse_url($url);if(!$parsed||!isset($parsed['host']))return false;
                                $scheme=$parsed['scheme'];$host=$parsed['host'];$port=isset($parsed['port'])?':'.$parsed['port']:'';
                                $path=isset($parsed['path'])?$parsed['path']:'/';
                                $path=preg_replace_callback('/[^A-Za-z0-9_\-\.~!$&\'()*+,;=:@\/]+/',function($m){return rawurlencode($m[0]);},$path);
                                $query=isset($parsed['query'])?'?'.$parsed['query']:'';return"$scheme://$host$port$path$query";}

                                function resolveUrl($rel,$base){$parsedBase=parse_url($base);$scheme=$parsedBase['scheme'];$host=$parsedBase['host'];$port=isset($parsedBase['port'])?':'.$parsedBase['port']:'';
                                    if(strpos($rel,'//')===0)return$scheme.':'.$rel;if(strpos($rel,'/')===0)return$scheme.'://'.$host.$port.$rel;
                                        $path=dirname($parsedBase['path']??'/');if($path==='\\')$path='/';return$scheme.'://'.$host.$port.$path.'/'.$rel;}

                                        function sendError($code,$message){if(headers_sent())return;if(ob_get_level())ob_clean();http_response_code($code);header('Content-Type: text/plain; charset=utf-8');exit($message);}

                                        function executeCurlRequest($url,$method,$headers,$body,$redirectCount){$ch=curl_init();$curlHeaders=[];
                                            foreach($headers as $name=>$value){$n=strtolower($name);if(!in_array($n,['host','connection','content-length','expect','transfer-encoding'])){$curlHeaders[]="$name: $value";}}
                                            $curlHeaders[]="User-Agent: ".USER_AGENT;$curlHeaders[]="Accept-Encoding: gzip, deflate";
                                            $options=[CURLOPT_URL=>$url,CURLOPT_CUSTOMREQUEST=>$method,CURLOPT_HTTPHEADER=>$curlHeaders,CURLOPT_RETURNTRANSFER=>false,CURLOPT_HEADER=>false,CURLOPT_FOLLOWLOCATION=>false,CURLOPT_CONNECTTIMEOUT=>CONNECT_TIMEOUT,CURLOPT_TIMEOUT=>TIMEOUT,CURLOPT_SSL_VERIFYPEER=>true,CURLOPT_SSL_VERIFYHOST=>2];
                                            if($body&&in_array($method,['POST','PUT','PATCH','DELETE'])){$options[CURLOPT_POSTFIELDS]=$body;}
                                            $pinData=resolveAndPinIp($url);if($pinData!==null){list($hostname,$port,$ip)=$pinData;$options[CURLOPT_RESOLVE]=["$hostname:$port:$ip"];}
                                            $headerLines=[];$httpCode=0;$isRedirect=false;$locationUrl='';$headersSentToClient=false;
                                            $headerFunc=function($ch,$data)use(&$headerLines,&$httpCode,&$isRedirect,&$locationUrl,&$headersSentToClient,$redirectCount){$line=trim($data);
                                                if(preg_match('/^HTTP\/[\d.]+\s+(\d+)/',$line,$m)){$httpCode=(int)$m[1];$isRedirect=in_array($httpCode,[301,302,303,307,308]);}
                                                if($line===''){if($isRedirect){foreach($headerLines as $h){if(preg_match('/^Location:\s*(.*)$/i',$h,$loc)){$locationUrl=trim($loc[1]);break;}}return strlen($data);}
                                                if(!$headersSentToClient){http_response_code($httpCode);foreach($headerLines as $h){if(empty($h))continue;if(preg_match('/^HTTP\//i',$h))continue;$parts=explode(':',$h,2);
                                                    if(count($parts)==2){$n=trim($parts[0]);$v=trim($parts[1]);$nLow=strtolower($n);if(in_array($nLow,['transfer-encoding','connection','keep-alive','proxy-connection']))continue;header("$n: $v",false);}}
                                                    if(ob_get_level())ob_flush();flush();$headersSentToClient=true;}$headerLines=[];return strlen($data);}$headerLines[]=$data;return strlen($data);};
                                                    $writeFunc=function($ch,$data)use($isRedirect,$headersSentToClient){if($isRedirect)return strlen($data);echo$data;if(ob_get_level())ob_flush();flush();return strlen($data);};
                                            $options[CURLOPT_HEADERFUNCTION]=$headerFunc;$options[CURLOPT_WRITEFUNCTION]=$writeFunc;curl_setopt_array($ch,$options);curl_exec($ch);$info=curl_getinfo($ch);$error=curl_error($ch);$finalCode=$info['http_code'];curl_close($ch);
                                            if($error){if(!$headersSentToClient)sendError(502,"Proxy Error: $error");return;}
                                            if($isRedirect&&$locationUrl){if(ob_get_level())ob_clean();$newUrl=resolveUrl($locationUrl,$url);
                                                if(!isUrlSafe($newUrl)){sendError(403,'Redirect target is not allowed');return;}followRedirects($newUrl,$redirectCount+1);}}

                                                function followRedirects($url,$redirectCount){if($redirectCount>MAX_REDIRECTS){sendError(508,'Too many redirects');return;}
                                                $method=$_SERVER['REQUEST_METHOD'];$headers=getClientHeaders();$body=file_get_contents('php://input');executeCurlRequest($url,$method,$headers,$body,$redirectCount);}

                                                function runProxy(){$target=$_REQUEST['url']??null;if(!$target){$target=$_SERVER['HTTP_X_TARGET_URL']??null;}
                                                if(!$target){sendError(400,'Missing target URL. Usage: ?url=http://example.com');return;}
                                                    $target=normalizeUrl($target);if(!$target){sendError(400,'Invalid URL format');return;}
                                                    if(!isUrlSafe($target)){sendError(403,'Access to local/private addresses is forbidden');return;}followRedirects($target,0);}

                                                    runProxy();
