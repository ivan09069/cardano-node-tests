"""Tests for Conway hard-fork."""

import logging

import allure
import pytest
from cardano_clusterlib import clusterlib

from cardano_node_tests.cluster_management import cluster_management
from cardano_node_tests.tests import common
from cardano_node_tests.tests import reqs_conway as reqc
from cardano_node_tests.tests.tests_conway import conway_common
from cardano_node_tests.utils import clusterlib_utils
from cardano_node_tests.utils import governance_utils
from cardano_node_tests.utils import helpers
from cardano_node_tests.utils.versions import VERSIONS

LOGGER = logging.getLogger(__name__)

pytestmark = pytest.mark.skipif(
    VERSIONS.transaction_era < VERSIONS.CONWAY,
    reason="runs only with Tx era >= Conway",
)


@pytest.fixture
def pool_user_lg(
    cluster_manager: cluster_management.ClusterManager,
    cluster_lock_governance: governance_utils.GovClusterT,
) -> clusterlib.PoolUser:
    """Create a pool user for "lock governance"."""
    cluster, __ = cluster_lock_governance
    key = helpers.get_current_line_str()
    name_template = common.get_test_id(cluster)
    return conway_common.get_registered_pool_user(
        cluster_manager=cluster_manager,
        name_template=name_template,
        cluster_obj=cluster,
        caching_key=key,
    )


class TestHardfork:
    """Tests for hard-fork."""

    @pytest.fixture(scope="class")
    def skip_hf_command(self):
        if not clusterlib_utils.cli_has("conway governance action create-hardfork"):
            pytest.skip(
                "The `cardano-cli conway governance action create-hardfork` command "
                "is not available."
            )

    @allure.link(helpers.get_vcs_link())
    @pytest.mark.long
    def test_hardfork(
        self,
        skip_hf_command: None,  # noqa: ARG002
        cluster_manager: cluster_management.ClusterManager,
        cluster_lock_governance: governance_utils.GovClusterT,
        pool_user_lg: clusterlib.PoolUser,
    ):
        """Test hardfork action.

        * create a "hardfork" action
        * check that DReps cannot vote during the bootstrap period
        * vote to disapprove the action
        * vote to approve the action
        * check that the action is ratified
        * try to disapprove the ratified action, this shouldn't have any effect
        * check that the action is enacted
        * check that it's not possible to vote on enacted action
        """
        cluster, governance_data = cluster_lock_governance
        temp_template = common.get_test_id(cluster)

        if not conway_common.is_in_bootstrap(cluster_obj=cluster):
            pytest.skip("The major protocol version needs to be 9.")

        init_return_account_balance = cluster.g_query.get_stake_addr_info(
            pool_user_lg.stake.address
        ).reward_account_balance

        # Create an action
        deposit_amt = cluster.conway_genesis["govActionDeposit"]
        anchor_url = "http://www.hardfork.com"
        anchor_data_hash = "5d372dca1a4cc90d7d16d966c48270e33e3aa0abcb0e78f0d5ca7ff330d2245d"
        prev_action_rec = governance_utils.get_prev_action(
            action_type=governance_utils.PrevGovActionIds.HARDFORK,
            gov_state=cluster.g_conway_governance.query.gov_state(),
        )

        _url = helpers.get_vcs_link()
        [
            r.start(url=_url)
            for r in (reqc.cli019, reqc.cip031a_07, reqc.cip031d, reqc.cip038_07, reqc.cip054_07)
        ]

        hardfork_action = cluster.g_conway_governance.action.create_hardfork(
            action_name=temp_template,
            deposit_amt=deposit_amt,
            anchor_url=anchor_url,
            anchor_data_hash=anchor_data_hash,
            protocol_major_version=10,
            protocol_minor_version=0,
            prev_action_txid=prev_action_rec.txid,
            prev_action_ix=prev_action_rec.ix,
            deposit_return_stake_vkey_file=pool_user_lg.stake.vkey_file,
        )
        [r.success() for r in (reqc.cip031a_07, reqc.cip031d, reqc.cip054_07)]

        tx_files_action = clusterlib.TxFiles(
            proposal_files=[hardfork_action.action_file],
            signing_key_files=[
                pool_user_lg.payment.skey_file,
            ],
        )

        # Make sure we have enough time to submit the proposal and the votes in one epoch
        clusterlib_utils.wait_for_epoch_interval(
            cluster_obj=cluster, start=1, stop=common.EPOCH_STOP_SEC_BUFFER - 20
        )

        tx_output_action = clusterlib_utils.build_and_submit_tx(
            cluster_obj=cluster,
            name_template=f"{temp_template}_action",
            src_address=pool_user_lg.payment.address,
            use_build_cmd=True,
            tx_files=tx_files_action,
        )

        out_utxos_action = cluster.g_query.get_utxo(tx_raw_output=tx_output_action)
        assert (
            clusterlib.filter_utxos(utxos=out_utxos_action, address=pool_user_lg.payment.address)[
                0
            ].amount
            == clusterlib.calculate_utxos_balance(tx_output_action.txins)
            - tx_output_action.fee
            - deposit_amt
        ), f"Incorrect balance for source address `{pool_user_lg.payment.address}`"

        action_txid = cluster.g_transaction.get_txid(tx_body_file=tx_output_action.out_file)
        action_gov_state = cluster.g_conway_governance.query.gov_state()
        _cur_epoch = cluster.g_query.get_epoch()
        conway_common.save_gov_state(
            gov_state=action_gov_state, name_template=f"{temp_template}_action_{_cur_epoch}"
        )
        prop_action = governance_utils.lookup_proposal(
            gov_state=action_gov_state, action_txid=action_txid
        )
        assert prop_action, "Hardfork action not found"
        assert (
            prop_action["proposalProcedure"]["govAction"]["tag"]
            == governance_utils.ActionTags.HARDFORK_INIT.value
        ), "Incorrect action tag"

        action_ix = prop_action["actionId"]["govActionIx"]

        # Check that DReps cannot vote
        with pytest.raises(clusterlib.CLIError) as excinfo:
            conway_common.cast_vote(
                cluster_obj=cluster,
                governance_data=governance_data,
                name_template=f"{temp_template}_no",
                payment_addr=pool_user_lg.payment,
                action_txid=action_txid,
                action_ix=action_ix,
                approve_cc=False,
                approve_drep=False,
                approve_spo=False,
            )
        err_str = str(excinfo.value)
        assert "(DisallowedVotesDuringBootstrap ((DRepVoter" in err_str, err_str

        # Vote & disapprove the action
        reqc.cip043_01.start(url=helpers.get_vcs_link())
        conway_common.cast_vote(
            cluster_obj=cluster,
            governance_data=governance_data,
            name_template=f"{temp_template}_no",
            payment_addr=pool_user_lg.payment,
            action_txid=action_txid,
            action_ix=action_ix,
            approve_cc=False,
            approve_spo=False,
        )
        reqc.cli019.success()

        # Vote & approve the action
        voted_votes = conway_common.cast_vote(
            cluster_obj=cluster,
            governance_data=governance_data,
            name_template=f"{temp_template}_yes",
            payment_addr=pool_user_lg.payment,
            action_txid=action_txid,
            action_ix=action_ix,
            approve_cc=True,
            approve_spo=True,
        )

        # Testnet will be using an unexpected protocol version, respin is needed
        cluster_manager.set_needs_respin()

        # Check ratification
        _cur_epoch = cluster.wait_for_new_epoch(padding_seconds=5)
        rat_gov_state = cluster.g_conway_governance.query.gov_state()
        conway_common.save_gov_state(
            gov_state=rat_gov_state, name_template=f"{temp_template}_rat_{_cur_epoch}"
        )
        rat_action = governance_utils.lookup_ratified_actions(
            gov_state=rat_gov_state, action_txid=action_txid
        )
        assert rat_action, "Action not found in ratified actions"
        reqc.cip043_01.success()

        # Disapprove ratified action, the voting shouldn't have any effect
        conway_common.cast_vote(
            cluster_obj=cluster,
            governance_data=governance_data,
            name_template=f"{temp_template}_after_ratification",
            payment_addr=pool_user_lg.payment,
            action_txid=action_txid,
            action_ix=action_ix,
            approve_cc=False,
            approve_spo=False,
        )

        assert rat_gov_state["nextRatifyState"]["ratificationDelayed"], "Ratification not delayed"
        reqc.cip038_07.success()

        # Check enactment
        _cur_epoch = cluster.wait_for_new_epoch(padding_seconds=5)
        enact_gov_state = cluster.g_conway_governance.query.gov_state()
        conway_common.save_gov_state(
            gov_state=enact_gov_state, name_template=f"{temp_template}_enact_{_cur_epoch}"
        )
        assert (
            enact_gov_state["currentPParams"]["protocolVersion"]["major"] == 10
        ), "Incorrect major version"

        enact_prev_action_rec = governance_utils.get_prev_action(
            action_type=governance_utils.PrevGovActionIds.HARDFORK,
            gov_state=enact_gov_state,
        )
        assert enact_prev_action_rec.txid == action_txid, "Incorrect previous action Txid"
        assert enact_prev_action_rec.ix == action_ix, "Incorrect previous action index"

        enact_deposit_returned = cluster.g_query.get_stake_addr_info(
            pool_user_lg.stake.address
        ).reward_account_balance

        assert (
            enact_deposit_returned == init_return_account_balance + deposit_amt
        ), "Incorrect return account balance"

        # Try to vote on enacted action
        with pytest.raises(clusterlib.CLIError) as excinfo:
            conway_common.cast_vote(
                cluster_obj=cluster,
                governance_data=governance_data,
                name_template=f"{temp_template}_enacted",
                payment_addr=pool_user_lg.payment,
                action_txid=action_txid,
                action_ix=action_ix,
                approve_drep=False,
                approve_spo=False,
            )
        err_str = str(excinfo.value)
        assert "(GovActionsDoNotExist" in err_str, err_str

        # Check action view
        governance_utils.check_action_view(cluster_obj=cluster, action_data=hardfork_action)

        # Check vote view
        if voted_votes.cc:
            governance_utils.check_vote_view(cluster_obj=cluster, vote_data=voted_votes.cc[0])
        governance_utils.check_vote_view(cluster_obj=cluster, vote_data=voted_votes.spo[0])
