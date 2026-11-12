use log::info;
use proxy_wasm::traits::*;
use proxy_wasm::types::*;
use serde::Deserialize;

proxy_wasm::main! {{
    proxy_wasm::set_log_level(LogLevel::Info);
    proxy_wasm::set_root_context(|_| -> Box<dyn RootContext> {
        Box::new(ShuffleRouterRoot {
            config: RouterConfig::default(),
        })
    });
}}

#[derive(Clone, Deserialize)]
struct RouterConfig {
    /// Total number of isolated cells in the fleet (e.g., 8 or 12).
    #[serde(default = "default_total_cells")]
    total_cells: usize,
    /// Virtual shard width k (e.g., k=3 for N=8 -> 56 virtual shards = 1.79% overlap;
    /// k=2 for N=12 -> 66 virtual shards = 1.52% overlap).
    #[serde(default = "default_shard_size")]
    shard_size: usize,
    /// Static cluster salt to prevent cross-environment hash correlation.
    #[serde(default = "default_salt")]
    hash_salt: String,
    /// Optional list of administratively drained cells pushed via plugin config.
    #[serde(default)]
    drained_cells: Vec<usize>,
}

fn default_total_cells() -> usize { 8 }
fn default_shard_size() -> usize { 3 }
fn default_salt() -> String { "gcp-cell-v1-prod".to_string() }

impl Default for RouterConfig {
    fn default() -> Self {
        Self {
            total_cells: default_total_cells(),
            shard_size: default_shard_size(),
            hash_salt: default_salt(),
            drained_cells: Vec::new(),
        }
    }
}

struct ShuffleRouterRoot {
    config: RouterConfig,
}

impl Context for ShuffleRouterRoot {}

impl RootContext for ShuffleRouterRoot {
    fn on_configure(&mut self, _plugin_configuration_size: usize) -> bool {
        if let Some(bytes) = self.get_plugin_configuration() {
            if let Ok(parsed) = serde_json::from_slice::<RouterConfig>(&bytes) {
                if parsed.total_cells > 0 && parsed.shard_size > 0 && parsed.shard_size <= parsed.total_cells {
                    self.config = parsed;
                }
            }
        }
        info!(
            "Initialized ShuffleRouterRoot: N={}, k={}",
            self.config.total_cells, self.config.shard_size
        );
        true
    }

    fn create_http_context(&self, _context_id: u32) -> Option<Box<dyn HttpContext>> {
        Some(Box::new(ShuffleRouterHttp {
            config: self.config.clone(),
        }))
    }

    fn get_type(&self) -> Option<ContextType> {
        Some(ContextType::HttpContext)
    }
}

struct ShuffleRouterHttp {
    config: RouterConfig,
}

impl Context for ShuffleRouterHttp {}

/// Deterministic 64-bit FNV-1a + fmix64 avalanche finalizer.
/// Zero external state or control-plane lookups -> preserves data-plane static stability.
fn fnv1a_64(input: &[u8], seed: u64) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for &byte in input {
        h ^= byte as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h ^= seed.wrapping_add(0x9e3779b97f4a7c15);
    h ^= h >> 33;
    h = h.wrapping_mul(0xff51afd7ed558ccd);
    h ^= h >> 33;
    h = h.wrapping_mul(0xc4ceb9fe1a85ec53);
    h ^= h >> 33;
    h
}

/// Computes a tenant's deterministic k-of-N virtual shard ("hand of cards") via partial Fisher-Yates.
fn compute_virtual_shard(tenant_id: &str, salt: &str, total_cells: usize, shard_size: usize) -> Vec<usize> {
    let mut deck: Vec<usize> = (0..total_cells).collect();
    let key = format!("{}:{}", salt, tenant_id);
    for i in 0..shard_size {
        let remaining = total_cells - i;
        let h = fnv1a_64(key.as_bytes(), i as u64);
        let pick = i + ((h % (remaining as u64)) as usize);
        deck.swap(i, pick);
    }
    deck.truncate(shard_size);
    deck
}

/// Parses an explicit operational cell header (`cell-00` .. `cell-NN`) when within `0..total_cells`.
fn parse_cell_override(raw: &str, total_cells: usize) -> Option<usize> {
    let trimmed = raw.trim().to_ascii_lowercase();
    let num_part = trimmed.strip_prefix("cell-")?;
    let idx = num_part.parse::<usize>().ok()?;
    if idx < total_cells {
        Some(idx)
    } else {
        None
    }
}

impl HttpContext for ShuffleRouterHttp {
    fn on_http_request_headers(&mut self, _num_headers: usize, _end_of_stream: bool) -> Action {
        let tenant_id = self
            .get_http_request_header("x-tenant-id")
            .unwrap_or_else(|| "anonymous-default".to_string());

        let retry_attempt: usize = self
            .get_http_request_header("x-retry-attempt")
            .and_then(|v| v.parse::<usize>().ok())
            .unwrap_or(0);

        let hand = compute_virtual_shard(
            &tenant_id,
            &self.config.hash_salt,
            self.config.total_cells,
            self.config.shard_size,
        );

        // Filter out administratively drained cells while keeping deterministic fallback
        let healthy_hand: Vec<usize> = hand
            .iter()
            .copied()
            .filter(|c| !self.config.drained_cells.contains(c))
            .collect();

        let active_pool = if healthy_hand.is_empty() { &hand } else { &healthy_hand };

        let explicit_cell = self
            .get_http_request_header("x-target-cell")
            .or_else(|| self.get_http_request_header("x-cell-override"))
            .and_then(|v| parse_cell_override(&v, self.config.total_cells));

        let selected_cell = explicit_cell.unwrap_or_else(|| active_pool[retry_attempt % active_pool.len()]);

        let ordered_hand = hand
            .iter()
            .map(|c| format!("cell-{:02}", c))
            .collect::<Vec<_>>()
            .join(",");

        let mut sorted_hand = hand;
        sorted_hand.sort_unstable();
        let shard_signature = sorted_hand
            .iter()
            .map(|c| format!("cell-{:02}", c))
            .collect::<Vec<_>>()
            .join(",");

        let target_cell = format!("cell-{:02}", selected_cell);
        let fleet_config = format!("N={},k={}", self.config.total_cells, self.config.shard_size);

        // Inject routing & observability headers consumed by Cloud Load Balancing and cell backends
        self.set_http_request_header("x-cell-id", Some(&target_cell));
        self.set_http_request_header("x-virtual-shard", Some(&shard_signature));
        self.set_http_request_header("x-ordered-hand", Some(&ordered_hand));
        self.set_http_request_header("x-fleet-config", Some(&fleet_config));

        Action::Continue
    }
}
