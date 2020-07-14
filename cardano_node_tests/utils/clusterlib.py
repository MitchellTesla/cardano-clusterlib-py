"""Wrapper for node-cli."""
import collections
import functools
import json
import subprocess
from copy import copy
from pathlib import Path

KeyPair = collections.namedtuple("KeyPair", ("vkey", "skey"))
CLIOut = collections.namedtuple("CLIOut", ("stdout", "stderr"))


class CLIError(Exception):
    pass


class ClusterLib:
    """Cluster Lib."""

    def __init__(self, network_magic, state_dir):
        self.network_magic = network_magic

        self.state_dir = Path(state_dir).expanduser().resolve()
        self.genesis_json = self.state_dir / "keys" / "genesis.json"
        self.genesis_utxo_vkey = self.state_dir / "keys" / "genesis-utxo.vkey"
        self.genesis_utxo_skey = self.state_dir / "keys" / "genesis-utxo.skey"
        self.genesis_vkey = self.state_dir / "keys" / "genesis-keys" / "genesis1.vkey"
        self.delegate_skey = self.state_dir / "keys" / "delegate-keys" / "delegate1.skey"
        self.pparams_file = self.state_dir / "pparams.json"

        self.check_state_dir()

        with open(self.genesis_json) as in_json:
            self.genesis = json.load(in_json)

        self.genesis_utxo_addr = self.get_genesis_addr(self.genesis_utxo_vkey)

        self.pparams = None
        self.refresh_pparams()

        self.slot_length = self.genesis["slotLength"]
        self.epoch_length = self.genesis["epochLength"]

    def check_state_dir(self):
        if not self.state_dir.exists():
            raise CLIError(f"The state dir `{self.state_dir}` doesn't exist.")

        for file_name in (
            self.genesis_json,
            self.genesis_utxo_vkey,
            self.genesis_utxo_skey,
            self.genesis_vkey,
            self.delegate_skey,
        ):
            if not file_name.exists():
                raise CLIError(f"The file `{file_name}` doesn't exist.")

    @staticmethod
    def cli(cli_args):
        p = subprocess.Popen(cli_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = p.communicate()
        if p.returncode != 0:
            raise CLIError(f"An error occurred running a CLI command `{p.args}`: {stderr}")
        return CLIOut(stdout, stderr)

    @staticmethod
    def prepend_flag(flag, contents):
        return sum(([flag, x] for x in contents), [])

    def query_cli(self, cli_args):
        return self.cli(
            [
                "cardano-cli",
                "shelley",
                "query",
                *cli_args,
                "--testnet-magic",
                str(self.network_magic),
            ]
        ).stdout

    def refresh_pparams(self):
        self.query_cli(["protocol-parameters", "--out-file", str(self.pparams_file)])
        with open(self.pparams_file) as in_json:
            self.pparams = json.load(in_json)

    def estimate_fee(self, txbody_file, txins=1, txouts=1, witnesses=1, byron_witnesses=0):
        self.refresh_pparams()
        stdout = self.cli(
            [
                "cardano-cli",
                "shelley",
                "transaction",
                "calculate-min-fee",
                "--testnet-magic",
                str(self.network_magic),
                "--protocol-params-file",
                str(self.pparams_file),
                "--tx-in-count",
                str(txins),
                "--tx-out-count",
                str(txouts),
                "--byron-witness-count",
                str(byron_witnesses),
                "--witness-count",
                str(witnesses),
                "--tx-body-file",
                str(txbody_file),
            ]
        ).stdout
        fee, *__ = stdout.decode().split(" ")
        return int(fee)

    def get_tx_fee(
        self, txins=None, txouts=None, certificates=None, signing_keys=None, proposal_file=None,
    ):
        txins = txins or []
        txouts_copy = copy(txouts) if txouts else []
        signing_keys = signing_keys or []
        certificates = certificates or []

        # TODO: calculate from current tip
        ttl = 100000

        # TODO: unhardcode genesis utxo
        change = (self.genesis_utxo_addr, 0)
        txouts_copy.append(change)

        txins_combined = [f"{x[0]}#{x[1]}" for x in txins]
        txouts_combined = [f"{x[0]}+{x[1]}" for x in txouts_copy]

        txin_args = self.prepend_flag("--tx-in", txins_combined)
        txout_args = self.prepend_flag("--tx-out", txouts_combined)
        cert_args = self.prepend_flag("--certificate-file", certificates)

        build_args_estimate = [
            "cardano-cli",
            "shelley",
            "transaction",
            "build-raw",
            "--ttl",
            str(ttl),
            "--fee",
            "0",
            "--out-file",
            "tx.body_estimate",
            *txin_args,
            *txout_args,
            *cert_args,
        ]

        if proposal_file:
            build_args_estimate.extend(["--update-proposal-file", proposal_file])

        # Build TX for estimate
        self.cli(build_args_estimate)

        # Estimate fee
        fee = self.estimate_fee(
            "tx.body_estimate",
            txins=len(txins),
            txouts=len(txouts_copy),
            witnesses=len(signing_keys),
        )

        return fee

    # TODO: add withdrawal support
    def build_tx(
        self,
        out_file="tx.body",
        txins=None,
        txouts=None,
        certificates=None,
        fee=0,
        proposal_file=None,
    ):
        txins = txins or []
        txouts_copy = copy(txouts) if txouts else []
        certificates = certificates or []

        # TODO: make change_address work - Needs CLI endpoint to query utxo by txid
        # If no change_address specified send to utxo change address
        # if not change_address:
        #    change_address = self.genesis_utxo_addr

        # TODO: calculate from current tip
        ttl = 100000

        # TODO: unhardcode genesis utxo
        change = (self.genesis_utxo_addr, 0)
        txouts_copy.append(change)

        deposit_amount = 0
        total_input_amount = functools.reduce(lambda x, y: x + y[2], txins, 0)
        txouts_copy[-1] = (self.genesis_utxo_addr, (total_input_amount - fee - deposit_amount))
        txins_combined = [f"{x[0]}#{x[1]}" for x in txins]
        txouts_combined = [f"{x[0]}+{x[1]}" for x in txouts_copy]

        txin_args = self.prepend_flag("--tx-in", txins_combined)
        txout_args = self.prepend_flag("--tx-out", txouts_combined)
        cert_args = self.prepend_flag("--certificate-file", certificates)

        build_args = [
            "cardano-cli",
            "shelley",
            "transaction",
            "build-raw",
            "--ttl",
            str(ttl),
            "--fee",
            str(fee),
            "--out-file",
            str(out_file),
            *txin_args,
            *txout_args,
            *cert_args,
        ]

        if proposal_file:
            build_args.extend(["--update-proposal-file", proposal_file])

        self.cli(build_args)

    def sign_tx(self, tx_body_file="tx.body", out_file="tx.signed", signing_keys=None):
        signing_keys = signing_keys or []
        key_args = self.prepend_flag("--signing-key-file", signing_keys)
        self.cli(
            [
                "cardano-cli",
                "shelley",
                "transaction",
                "sign",
                "--tx-body-file",
                str(tx_body_file),
                "--out-file",
                str(out_file),
                "--testnet-magic",
                str(self.network_magic),
                *key_args,
            ]
        )

    def submit_tx(self, tx_file="tx.signed"):
        self.cli(
            [
                "cardano-cli",
                "shelley",
                "transaction",
                "submit",
                "--testnet-magic",
                str(self.network_magic),
                "--tx-file",
                str(tx_file),
            ]
        )

    def get_payment_address(self, payment_vkey, stake_vkey=None):
        if not payment_vkey:
            raise CLIError("Must set payment key.")

        cli_args = ["--payment-verification-key-file", str(payment_vkey)]
        if stake_vkey:
            cli_args.extend("--stake-verification-key-file", str(stake_vkey))

        return (
            self.cli(
                [
                    "cardano-cli",
                    "shelley",
                    "address",
                    "build",
                    "--testnet-magic",
                    str(self.network_magic),
                    *cli_args,
                ]
            )
            .stdout.rstrip()
            .decode("ascii")
        )

    def get_genesis_addr(self, vkey_path):
        return (
            self.cli(
                [
                    "cardano-cli",
                    "shelley",
                    "genesis",
                    "initial-addr",
                    "--testnet-magic",
                    str(self.network_magic),
                    "--verification-key-file",
                    str(vkey_path),
                ]
            )
            .stdout.rstrip()
            .decode("ascii")
        )

    def get_utxo(self, address):
        self.query_cli(["utxo", "--address", address, "--out-file", "utxo.json"])
        with open("utxo.json") as in_json:
            utxo = json.load(in_json)
        return utxo

    def get_tip(self):
        return json.loads(self.query_cli(["tip"]))

    def create_payment_key_pair(self, destination_dir, key_name):
        destination_dir = Path(destination_dir).expanduser()
        skey = destination_dir / f"{key_name}.skey"
        vkey = destination_dir / f"{key_name}.vkey"
        self.cli(
            [
                "cardano-cli",
                "shelley",
                "address",
                "key-gen",
                "--verification-key-file",
                vkey,
                "--signing-key-file",
                skey,
            ]
        )
        return KeyPair(vkey, skey)

    def create_stake_key_pair(self, destination_dir, key_name):
        destination_dir = Path(destination_dir).expanduser()
        skey = destination_dir / f"{key_name}.skey"
        vkey = destination_dir / f"{key_name}.vkey"
        self.cli(
            [
                "cardano-cli",
                "shelley",
                "stake-address",
                "key-gen",
                "--verification-key-file",
                vkey,
                "--signing-key-file",
                skey,
            ]
        )
        return KeyPair(vkey, skey)

    def build_payment_address(self, payment_vkey):
        return (
            self.cli(
                [
                    "cardano-cli",
                    "shelley",
                    "address",
                    "build",
                    "--payment-verification-key-file",
                    str(payment_vkey),
                    "--testnet-magic",
                    str(self.network_magic),
                ]
            )
            .stdout.rstrip()
            .decode("ascii")
        )

    def build_stake_address(self, stake_vkey):
        return (
            self.cli(
                [
                    "cardano-cli",
                    "shelley",
                    "stake-address",
                    "build",
                    "--stake-verification-key-file",
                    str(stake_vkey),
                    "--testnet-magic",
                    str(self.network_magic),
                ]
            )
            .stdout.rstrip()
            .decode("ascii")
        )

    def delegate_stake_address(self, stake_addr_skey, pool_id, delegation_fee):
        cli_args = [
            "cardano-cli",
            "shelley",
            "stake-address",
            "delegate",
            "--signing-key-file",
            str(stake_addr_skey),
            "--pool-id",
            str(pool_id),
            "--delegation-fee",
            str(delegation_fee),
        ]

        stderr = self.cli(cli_args).stderr
        if stderr and "runStakeAddressCmd" in stderr.decode():
            cmd = " ".join(cli_args)
            raise CLIError(f"command not implemented yet;\ncommand: {cmd}\nresult: {stderr}")

    def get_stake_address_info(self, stake_addr):
        output_json = json.loads(self.query_cli(["stake-address-info", "--address", stake_addr]))
        delegation = output_json[stake_addr]["delegation"]
        reward_account_balance = output_json[stake_addr]["rewardAccountBalance"]

        StakeAddrInfo = collections.namedtuple(
            "StakeAddrInfo", ("delegation", "reward_account_balance", "stake_addr_info")
        )
        return StakeAddrInfo(delegation, reward_account_balance, output_json)

    def send_tx_genesis(
        self, txouts=None, certificates=None, signing_keys=None, proposal_file=None,
    ):
        txouts = txouts or []
        certificates = certificates or []
        signing_keys = signing_keys or []

        utxo = self.get_utxo(address=self.genesis_utxo_addr)
        total_input_amount = 0
        txins = []
        for k, v in utxo.items():
            total_input_amount += v["amount"]
            txin = k.split("#")
            txin = (txin[0], txin[1])
            txins.append(txin)

        # TODO: calculate from current tip
        signing_keys.append(str(self.genesis_utxo_skey))
        utxo = self.get_utxo(address=self.genesis_utxo_addr)
        txins = []
        for k, v in utxo.items():
            txin = k.split("#")
            txin = (txin[0], txin[1], v["amount"])
            txins.append(txin)

        # Build, Sign and Send TX to chain
        try:
            fee = self.get_tx_fee(txins, txouts, certificates, signing_keys, proposal_file)
            self.build_tx(
                txins=txins,
                txouts=txouts,
                certificates=certificates,
                fee=fee,
                proposal_file=proposal_file,
            )
            self.sign_tx(signing_keys=signing_keys)
            self.submit_tx()
        except CLIError as err:
            raise CLIError(
                f"Sending a genesis transaction failed!\n"
                f"utxo: {utxo}\n"
                f"txins: {txins} txouts: {txouts} signing keys: {signing_keys}\n{err}"
            )

    def get_current_epoch_no(self):
        tip = self.get_tip()
        return int(tip["slotNo"] / self.epoch_length)

    def submit_update_proposal(self, cli_args, epoch=None):
        self.cli(
            [
                "cardano-cli",
                "shelley",
                "governance",
                "create-update-proposal",
                *cli_args,
                "--out-file",
                "update.proposal",
                "--epoch",
                str(epoch or self.get_current_epoch_no()),
                "--genesis-verification-key-file",
                str(self.genesis_vkey),
            ]
        )
        self.send_tx_genesis(
            proposal_file="update.proposal", signing_keys=[str(self.delegate_skey)],
        )
